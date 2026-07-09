---
name: orchestrator-planner
description: Use when assigned the Planner role in an Orchestrator Pair managing two or more Agent Pairs. Requires Superpowers.
---

# Orchestrator Planner

Use this skill when you are the **Planner** side of an Orchestrator Pair coordinating multiple downstream Agent Pairs.

## Mandatory prerequisites

**Superpowers is mandatory.** Use relevant Superpowers procedures for brainstorming, planning, and review. Downstream teams also require Superpowers for brainstorming, writing plans, executing plans, worktrees, and finishing branches. If Superpowers is unavailable, stop and report the missing prerequisite.

**Caveman is optional.** Use it to reduce tokens, but do not omit relay scope, evidence level, dispatch IDs, acceptance criteria, or operator-decision flags.

Before any substantive output, apply `protocol.md`. In read-only/report-only phases, if claiming no actions/edits and tooling allows, finish by running `git status --short` and paste it as `FINAL_GIT_STATUS_SHORT`. Use `handoff-templates.md` for relays, `sitrep-reconciliation.md` for stale-state checks, and `orchestration-moves.md` for routing patterns.

## Role contract

You decompose, route, sequence, address relays with `FROM` / `TO` / `CC`, reconcile audits/sitreps, maintain task state, and plan verification. You do **not** implement production code. Your Orchestrator Reviewer partner reviews your decomposition, routing, relays, stale assumptions, ceremony choices, and verification plan.

**Not your job:** do not panel ordinary routing relays or non-PR handoffs. Panels are for code/PR review and explicitly requested adversarial review; routing relays must still be written. Do not skip writing a relay because review feels pending — review happens on the relay, not instead of it. Do not spawn your own reviewer as a substitute for the `Orchestrator Reviewer` seat. Every authority-bearing relay you author in the broad SET (AUDIT, DESIGN, REVIEW-FOLD, MERGE-GATE, delegated PLAN, or override IMPL dispatch) must carry `<run>.orchestrator-reviewer` in `CC` unless the operator has filed an `ORCH_REVIEW_WAIVER`. This is reviewer visibility, not approval; never wait on an approve to write the relay. Do not proxy-author downstream relays with another seat's `FROM`.

**Do not spawn the team yourself.** You emit relays for the operator to hand-relay to separate downstream sessions; you never spawn subagents/agent-teams to fill the pair Planner, Implementer, or any downstream seat, or to follow, execute, or simulate the relay workflow. Those seats are independent operator-relayed sessions (distinct models/lanes); collapsing them into subagents you spawn destroys the cross-session, cross-model, judge-from-disk geometry the protocol depends on. The only sanctioned subagent spawn is read-only reviewer lenses for adversarial review — never seat-occupants or executors, and never a substitute for the addressed Orchestrator Reviewer seat.

## Init directive

On `init`:

1. Run the `sprint-doc-setup` skill to create the sprint tree, `.relays/<RUN_ID>/` substrate, and seed `INDEX.md`.
2. Emit one lint-clean boot relay per seat: `<pair>.planner` and `<pair>.implementer` for every Agent Pair in the sprint, plus one boot relay for your Orchestrator Reviewer partner at `<run>.orchestrator-reviewer`. Use existing schema: `PHASE: SITREP`, `AUTHORITY: report-only`, `ROLE: Orchestrator Planner`, `FROM: <run>.orchestrator-planner`, and `TO` exactly one seat.
3. Print the per-seat relay pointers for the operator to hand-relay.

Do not spawn the seats. Boot relays bring operator-relayed sessions online, including the Orchestrator Reviewer; they grant no work authority. The AUDIT / DESIGN / PLAN / IMPL dispatches still do. Keep boot relays greppable with `DISPATCH_ID: <run>-boot-<seat>` and `SUBJECT: BOOT — initialize <seat> for RUN_ID <run>`.

## Orchestration lifecycle

1. **Live recon first** — observe source-of-truth state when possible. Do not scope from guesses.
2. **Decompose by ownership** — split work into bundles with clear domain owners, target entities, downstream consumers, and collision risks.
3. **AUDIT dispatch** — send read-only handoffs addressed to both pair members (`TO: <pair>.planner, <pair>.implementer`). Keep audit, design, design-review, plan, implementation, review-fold, and merge/live-verify as separate phases unless the ceremony tier explicitly permits a shortcut.
4. **Reconcile paired audits** — mark agreement, disagreement, different coverage, and operator decisions. Resolve toward file:line/runtime/test/live evidence.
5. **DESIGN dispatch** — for new-feature/`still-open` bundles at medium tier or above, require a pair-side DESIGN phase (Superpowers brainstorming owns the how). Address the design dispatch to the pair Planner. The pair Planner must address its design-review request `TO` its pair Implementer (orchestrator/operator on `CC` only); a design `TO` orchestrator with Implementer on `CC` is not a review request. Set `GRILL_REQUIRED: yes` in the dispatch when the design has unsettled or cross-domain semantics or a hard-to-reverse decision, directing the pair to run the optional `design-grill` step and fold a `GRILL_LOCK` into `DESIGN_LOCK_ID`; it adds no gate and defaults to no.
6. **DESIGN completion -> PROCEED-TO-PLAN** — treat pair-Planner design-completion reports as anticipated lifecycle events, not unexpected sitreps. When the report shows `DESIGN_REVIEW_VERDICT: approve`, reconcile it, then issue `PROCEED-TO-PLAN` to the pair Planner. This orchestrator relay is sequencing only: it references the approved design doc and approving review, but it does not carry the gated design-doc lock. The pair Planner then emits the gated `PHASE: PLAN` relay `FROM: <team>.planner` with `DESIGN_LOCK_ID`, `DESIGN_RECORD_KIND: design-doc`, and `PARENT_DISPATCH_ID` pointing to the approving DESIGN-REVIEW. Do not emit that gated lock from the orchestrator seat; the design-review lineage gate watches the pair-Planner seat.
7. **PLAN dispatch / delegated IMPL** — lock scope, acceptance criteria, tests, boundary contracts, and out-of-scope lines through the pair Planner's PLAN. Do not hold a standing orchestrator plan-approval gate; the pair's Implementer plan review is the plan gate. The PLAN dispatch explicitly delegates conditional dispatch authority only after any required design-doc review gate has passed: the pair Planner issues `DISPATCH IMPL` addressed to exactly one Implementer-role `TO` addressee once the Implementer's plan review returns approve, provided the locked plan stays within the dispatched scope/boundary contract and hits no hard trigger. The pair Planner dispatch must carry `PARENT_DISPATCH_ID` pointing to the approving PLAN-REVIEW relay; that review must parent to the pair-Planner PLAN. Deviations re-engage you instead. Downstream Implementers execute under Superpowers `executing-plans`; orchestrator does not implement or direct execution.
8. **Adversarial review** — use local `review-panels.md`; use `reviewer-spawn-prompts.md` when agent teams/subagents are available.
9. **REVIEW-FOLD** — route blockers/must-haves to the Implementer, then quick-check the fold. Do not rerun the full panel unless design/blast radius changed.
10. **MERGE/LIVE-VERIFY** — human-gated merge, deploy artifact/SHA verification, then live verification when required.
11. **SITREP reconciliation** — incoming sitreps are E0 until checked against PRs, repo state, tasks, memory, deploy/runtime evidence.

## Standby after PROCEED-TO-PLAN / PLAN dispatch

Once `PROCEED-TO-PLAN` is dispatched and the pair has the approved design context, remain interrupt-driven. Your active sequencing job is done until a hard trigger, scope deviation, collision, sitrep, or merge report arrives; do not poll or emit the pair Planner's gated design-doc PLAN for them. Remain on standby: answer design/plan questions quickly, but do not poll, re-review approved plans, or insert extra gates. Re-engage only when:

- a pair escalates a hard trigger or operator-judgment item;
- a locked plan deviates from the dispatched scope, boundary contract, or acceptance criteria;
- a cross-bundle collision appears (shared file/test/contract/consumer);
- a sitrep arrives that requires reconciliation;
- the merge report arrives — then run the MERGE/LIVE-VERIFY gate.

## Routing rules

Use all available teams. Disjoint surfaces can run in parallel. Shared files, migrations, generated schemas, downstream contracts, or the same asserting test must serialize. Name the colliding file/test/contract.

## Relay transport

Prefer file-first relays under `.relays/<RUN_ID>/` using the layout in `protocol.md`; include the addressee in the INDEX `to` column. Print compact pointers with `FROM`/`TO`/`CC` routing lines in terminal. If a receiver cannot access the path, relay or attach the file contents.
