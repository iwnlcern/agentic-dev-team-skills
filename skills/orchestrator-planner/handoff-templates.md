# Orchestrator Handoff Templates

The orchestrator-review visibility gate requires orchestrator-planner authority relays in the broad SET to CC `<run>.orchestrator-reviewer` unless the run has an operator-authored `ORCH_REVIEW_WAIVER`. This is visibility, not approval.

Use these relay-ready templates. Always write the routing relay; do not replace a non-PR routing relay with a review panel. Panels are for code/PR review or explicitly requested adversarial review of a written relay. Write full relays to the file-first transport described in `protocol.md`, preferably `.relays/<RUN_ID>/<DISPATCH_ID>/<PHASE>-<ROLE>-<timestamp>.md` (the `<timestamp>` is the real clock time at authoring — see the timestamp policy in `protocol.md`), then print only a compact pointer, the relay routing lines (`FROM`, `TO`, and `CC` when present), plus a 3–6 line summary. If the receiver cannot access the path, relay or attach the file contents.

Canonical inline pointer shape:

```text
RELAY: .relays/<RUN_ID>/<DISPATCH_ID>/PLAN-planner-<ts>.md
FROM: pair-1.planner
TO: pair-1.implementer
CC: orchestrator, <run>.orchestrator-reviewer
<3-6 line summary>
```

Orchestrator never implements. Downstream pairs follow role contracts:

- Planner audits, designs, plans, reviews, and may issue delegated `DISPATCH IMPL` only when the PLAN dispatch explicitly delegates it and the protocol conditions are met.
- Implementer independently audits, answers design questions, reviews plans, implements only after the exact literal token `DISPATCH IMPL` appears bare, unfenced, un-backticked, alone on its own line, and is addressed to that Implementer in `TO` for relay files under the active run's RELAY_ROOT or directly to that single Implementer in a direct message; then folds review findings. Merge remains separate: only a valid MERGE-GATE field-form authorization with exactly one operational authorization-verdict line, or a bare, own-line `DISPATCH MERGE` addressed to exactly one Implementer, authorizes merge.

Use minimal headers by default, including `FROM` / `TO` / `CC` for orchestrator-tier or multi-pair relays. Add `PARENT_DISPATCH_ID` for pair-Planner implementation dispatches, substantive IMPL action reports, and every relay that participates in a lineage gate; `IN_REPLY_TO` is local/display-only and never a gate input. Add tier/risk-dependent fields when they affect lineage, routing, merge, or verification. When `relay-lint` is available, lint substantive relay files before delegated dispatch, merge, or adapter/CI consumption.


Operator no-reviewer waiver, only when the run genuinely has no Orchestrator Reviewer:

```text
ROLE: Reviewer
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

ROLE: Downstream Agent Pair
PHASE: AUDIT
AUTHORITY: read-only
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + for what>
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

ROLE: Downstream Agent Pair
PHASE: DESIGN
AUTHORITY: design-only for Planner; read-only challenge/answers for Implementer
DISPATCH_ID: <id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + product/design decisions>
FROM: orchestrator
TO: <team>.planner
CC: <team>.implementer, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <audit/reconcile dispatch id>
DESIGN_DOC_ID: <design id/title to create>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha | unknown>

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
HUMAN_GATE_REQUIRED: <yes/no + for what>
FROM: <team>.implementer
TO: <team>.planner
CC: <operator | orchestrator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <DESIGN relay dispatch id>
DESIGN_DOC_ID: <design id/title/hash>
DESIGN_REVIEW_VERDICT: <approve | must-revise | reject-narrow | human-decision-required>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>

Current scope: read-only design review only.
Check: target entity, boundary contract, acceptance criteria draft, rejected alternatives, operator decisions/defaults, and unresolved questions.
Return one verdict: approve / must-revise / reject-narrow / human-decision-required.
FINAL_GIT_STATUS_SHORT: <paste output or unavailable — reason>
```

## Template I — DESIGN request from pair Planner to pair Implementer

Use this after a pair Planner has produced a real design doc and needs the pair Implementer to perform the required read-only DESIGN-REVIEW. This is the Planner request. Template C is the Implementer response.

```text
## Team <id> — <bundle>: DESIGN-REVIEW REQUEST

ROLE: Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: <design request id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + for what>
FROM: <team>.planner
TO: <team>.implementer
CC: <orchestrator | operator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <DESIGN relay dispatch id from orchestrator/operator>
DESIGN_DOC_ID: <design id/title/hash>
DESIGN_RECORD_KIND: design-doc
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>

Current scope for the `TO` addressee: read-only DESIGN-REVIEW. No source/test edits, no implementation branches, no commits, no PRs.

Anti-pattern: `TO: orchestrator` with `<team>.implementer` only on `CC` is not a DESIGN-REVIEW request. CC is context only; it grants no phase authority and creates no review obligation. If this relay is misaddressed, reissue it with `TO: <team>.implementer`.

Design doc to review:
- DESIGN_DOC_ID: <design id/title/hash>
- Location / relay pointer: <path or attachment>
- Selected option:
- Rejected alternatives:
- Boundary contract:
- Acceptance criteria draft:
- Operator decisions/defaults:
- Open questions:

Requested response:
Use Template C (`PHASE: DESIGN-REVIEW`, `FROM: <team>.implementer`, `TO: <team>.planner`, `PARENT_DISPATCH_ID: <this dispatch id>`, same `DESIGN_DOC_ID`) and return `DESIGN_REVIEW_VERDICT: approve | must-revise | reject-narrow | human-decision-required`.

FINAL_GIT_STATUS_SHORT: <paste output or unavailable — reason>
```

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
HUMAN_GATE_REQUIRED: <yes/no + remaining operator decisions>
FROM: <team>.planner
TO: orchestrator
CC: <team>.implementer, <operator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <approving DESIGN-REVIEW dispatch id>
DESIGN_DOC_ID: <approved design id/title/hash>
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

ROLE: Downstream Agent Pair
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + for what>
FROM: orchestrator
TO: <team>.planner
CC: <team>.implementer, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <design-completion SITREP/report id for design-doc path; otherwise audit/design dispatch id>
APPROVED_DESIGN_DOC_ID: <approved design doc id/hash | none>
APPROVING_DESIGN_REVIEW_DISPATCH_ID: <approving DESIGN-REVIEW dispatch id | none>
PLAN_LOCK_ID: <plan id/title to create>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha | unknown>
TARGET_BRANCH: <branch>
DELEGATED_DISPATCH_AUTHORITY: <yes/no>
DELEGATED_DISPATCH_CONDITIONS: Implementer plan review = approve; pair-Planner dispatch PARENT_DISPATCH_ID points to that approve relay; that review parents to this pair-Planner PLAN; SCOPE_DIFF_RESULT = all-in; no hard trigger; no boundary-contract deviation; no cross-bundle collision.

Implementer phase scope — PLAN-REVIEW after plan is drafted.
Current scope: answer design questions, review Planner's plan, findings inline.
Not in current scope: source/test edits, implementation branches, commits, PRs, scaffolding, or prototype implementation.
Implementation begins only after a current relay under the active run's RELAY_ROOT contains the exact literal token `DISPATCH IMPL` bare, unfenced, un-backticked, alone on its own line, and addressed to the Implementer in `TO`, or a direct message to that single Implementer contains the same bare own-line token. Urgency, “just fix it now,” or “ship today” is not dispatch; inline, quoted, fenced, CC-only, cross-read, or non-addressee mentions are inert.

Approved design context:
<the decided fork, with APPROVED_DESIGN_DOC_ID and approving DESIGN-REVIEW reference when available>.

This `PROCEED-TO-PLAN` relay is sequencing only. It does not carry the gated design-doc lock. The pair Planner emits the gated PLAN from `FROM: <team>.planner` with `DESIGN_LOCK_ID`, `DESIGN_RECORD_KIND: design-doc`, and `PARENT_DISPATCH_ID` pointing to the approving DESIGN-REVIEW relay.

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

Use this for operator/orchestrator-issued implementation dispatch, or when delegation is disabled. When delegation is enabled and conditions are met, the pair Planner may issue the exact token as a bare, unfenced, un-backticked own line after SCOPE_DIFF all-in and Implementer approve.

```text
## Team <id> — <bundle>: implementation dispatch

ROLE: Downstream Implementer
PHASE: IMPL
AUTHORITY: implementation
DISPATCH_ID: <id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: merge requires human/operator gate
FROM: <orchestrator | operator | team.planner>
TO: <team>.implementer
CC: <team>.planner, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
PARENT_DISPATCH_ID: <approving PLAN-REVIEW dispatch id for pair-Planner delegated dispatch; plan/parent id for direct operator/orchestrator dispatch>
DESIGN_LOCK_ID: <design id/title/hash | none>
PLAN_LOCK_ID: <locked plan id/title/hash>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha>
TARGET_BRANCH: <branch>

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

## Template E — REVIEW-FOLD handoff

```text
## Team <id> — <bundle>: REVIEW-FOLD

ROLE: Downstream Implementer
PHASE: REVIEW-FOLD
AUTHORITY: fold-in-only on existing PR branch
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: merge requires human/operator gate
FROM: <planner | orchestrator | operator>
TO: <team>.implementer
CC: <team>.planner, <run>.orchestrator-reviewer, <operator | none>
PARENT_DISPATCH_ID: <impl dispatch id>
DESIGN_LOCK_ID: <design id/title/hash | none>
PLAN_LOCK_ID: <locked plan id/title/hash>
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

## Template F — MERGE/LIVE-VERIFY gate

```text
## Team <id> — <bundle>: MERGE/LIVE-VERIFY GATE

ROLE: <Planner | Orchestrator Planner | Implementer as assigned>
PHASE: MERGE-GATE -> LIVE-VERIFY
AUTHORITY: merge-gated; live-verify
DISPATCH_ID: <id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: yes — merge/deploy/live-verification judgment
FROM: <team>.planner
TO: operator
CC: <team>.implementer, <run>.orchestrator-reviewer, <orchestrator | none>
DESIGN_LOCK_ID: <design id/title/hash | none>
PLAN_LOCK_ID: <locked plan id/title/hash>
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
- Merge-authorization relay: <relay id/path or pending>
- If this relay authorizes the Implementer to merge, route it as `FROM: operator|orchestrator|<orchestrator-planner>` and `TO: <team>.implementer`, then include the operative token as a bare, unfenced, un-backticked own line:

DISPATCH MERGE

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
<complete | merge-blocked | merged-not-deployed | deployed-not-live-verified | failed-live-verification | human-decision-required>
```

## Template G — BOOT relay for `init`

Use this only during the orchestrator-planner `init` directive, after `sprint-doc-setup` has prepared the sprint tree and `.relays/<RUN_ID>/` substrate. Emit one relay per downstream seat and print the file pointer for the operator to hand-relay. This is an onboarding relay, not work authorization.

```text
## BOOT — initialize <seat> for RUN_ID <run>

ROLE: Orchestrator Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: <run>-boot-<seat>
PARENT_DISPATCH_ID: <run>-boot
RUN_ID: <run>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: <run>.orchestrator-planner
TO: <pair>.<planner|implementer> | <run>.orchestrator-reviewer
CC: operator
SUBJECT: BOOT — initialize <seat> for RUN_ID <run>

You are <pair>.<role> or <run>.orchestrator-reviewer for RUN_ID <run>.
Load <agent-pair-planner | agent-pair-implementer | orchestrator-reviewer>.
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
PHASE: <AUDIT | DESIGN | DESIGN-REVIEW | PLAN | PLAN-REVIEW | IMPL | REVIEW-FOLD | MERGE-GATE | LIVE-VERIFY | SITREP | RECONCILE>
AUTHORITY: report-only
DISPATCH_ID: <id>
CEREMONY_TIER: <tier>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + for what>
FROM: <sender owner.role | orchestrator | operator>
TO: <recipient owner.role | orchestrator | operator>
CC: <boundary-adjacent owner.role | none>
PARENT_DISPATCH_ID:
DESIGN_LOCK_ID:
PLAN_LOCK_ID:
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
