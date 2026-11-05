# Agentic Dev Team Protocol

Use this rulebook for agent judgment; the engine decides relay grammar and admission.
Role scope: Master Planner (`master-planner`), Master Reviewer (`master-reviewer`), Domain Planner (`domain-planner`), Domain Reviewer (`domain-reviewer`), Pair Planner (`pair-planner`), Pair Implementer (`pair-implementer`) and legacy Planner (`planner`).

## Scope note

This protocol defines workflow scope only.
It does not ask any agent to bypass platform, tool, repository, safety, or operator restrictions.
If an instruction conflicts with a higher-priority policy or tool boundary, pause and report the conflict.

## Host adaptation

Claude Code is the default host and has no adaptation file.
If your harness is something else, read the matching `harness-<host>.md` beside this protocol before acting — `harness-codex.md` for OpenAI Codex.
Model-specific files (`model-<family>.md`) follow the same pattern and ship empty: one may be added only when it cites a reproducible failure, and it is deleted when the failure stops reproducing.
Host and model files map protocol mechanics onto a host; they never change the protocol, and on conflict the protocol wins.

## Plan contract

A plan is a contract for implementation, not implementation written by the Planner.
Name the locked design, constraints, scope/exclusions, criteria, boundary contracts and verification target.
Tasks name files/anchors, interfaces, tests/assertions/controls, commands/outcomes, dependencies and commit boundaries.
Do not put implementation bodies, full test bodies or step-by-step operational scripts into a plan.
Use a fenced block only for a signature, a command and expected outcome, a file:line pointer or a schema excerpt.
Review task order, interfaces, tests and scope; do not ask for code bodies.
Code correctness belongs to implementation and PR review.
Support codebase claims with probes; reviewers independently test completeness with controlled searches.

Execution binds scope, exclusions, criteria, contracts, task boundaries, interfaces, test behaviors and assertions, and TDD order.
Snippet bytes are not bound.
The Implementer writes the code and tests; adding a test or discriminating control is in bounds.
When a snippet contradicts a declared interface or criterion, the interface or criterion wins.
Report unbound differences per task.
A difference that changes a bound item requires the Implementer to stop and relay a blocker before proceeding.
After approval, a correction that leaves bound items unchanged gets an erratum and delta review only; a bound-item change requires reissue.
Do not edit an approved plan to disguise implementation drift.
Plan-shape and revision warnings prompt contract review; they prove no defect.

## Working documents and evidence

File substantive handoffs; keep the design conversation inline and file its resulting lock.
Revise the one working copy of a design or plan in place; do not create a new `-rN` file for each revision.
Review snapshots and the lock mark belong to DD-v3-engine § 17.
Point to files and lines instead of copying long passages.
Keep and reread evidence on disk.
Do not edit or delete filed relays or daemon projections.
Ask operator questions plainly, with the decision and consequences.
Use a `KIND: QUESTION` relay to `operator` for operator decisions and `NEEDS: answer` for seat-to-seat questions (DD-v3-engine § 3).
Keep your own author identity; never proxy-author another seat's authority.
A digest acknowledgment is legitimate only in its matching approval or direct-override lineage; a register entry alone is not approval.
After filing, print your FROM and 3–6 summary lines, then terminal RELAY/TO/CC blocks in filing order, with nothing after them.
Use the filed header addressees verbatim; omit absent CC; if the receiver cannot access the file, relay or attach its contents.
Use the real authoring clock; never invent a timestamp.
Panels report to their owner; they do not replace addressed reviewers.

## Authority check before acting

Check the filed grant before implementation, changed-evidence resume or merge; findings grant nothing.
Run `tools/relay authority check <relay> --seat <own address> --action implement|merge --root <root>`; `_check` in `tools/relay_engine/authority.py` decides permission.
Act only on an allowed answer matching the relay, seat and action, with exit 0.
Every nonzero exit stops the act; a denial, a result that could not be evaluated, no result at all, and a client without this command are relayed as blockers to the governing seat.
Quote the result and nonempty external limits, or stderr and exit status if no result.
Do not retry into a pass or fall back to reading the relay by eye.
A command failure never turns a managed root into a hand-authored root.
Retain the command, its output or stderr code, and exit status for every check actually run.
A later allowed answer does not prove permission at an earlier act.
If an artifact, amendment, governing relay or cycle state changes, check again before resuming; unknown counts as changed.
Keep earlier checks as evidence and cite the latest check preceding the reported act.
On a managed root, the sender must file a chat go-ahead before action.
On a hand-authored root, report why the check is unavailable; never invent success.
Implementation permission and green tests do not grant merge permission.
Do not self-authorize a merge or treat a review verdict as a merge grant.

## Work in progress

A relay arrival never ends work in progress: read the relay file, then carry on with what you were doing; a seat with nothing in progress and no `TO` relay to act on ends its turn as usual.
A `TO` addressee acts under the relay's own authority, and a seat with nothing in progress acts on it at once.
For a seat busy on other addressed work, a `TO` relay that names the dispatch, bundle, branch or PR of that work, or that halts it, applies at once and is never turned into a question.
For that seat, any other `TO` relay waits for its next safe checkpoint: a finished command, a commit, or a filed relay.
The seat asks its governing seat which comes first only when such a relay says, or plainly needs, that it go first, and it carries on with the work in progress while it waits for the answer.
Here the governing seat is the seat whose addressed relay authorized the work in progress, or the operator when that seat is the operator or when there is none.
Before taking up another relay at a checkpoint, the seat records where the interrupted work stands and its next step somewhere that outlasts the conversation, such as a file it keeps for that work.
Once that relay is handled, the seat returns to the interrupted work unless that relay halted or replaced it.
A `CC` arrival is context only: it grants no authority and asks for no reply, so nothing is owed or filed in response to it.
A seat whose role gives it a standing duty toward copied relays still acts on that duty and files under it, not as a reply the copy asked for.

## Addressing and delivery

A CC'd relay can still be evidence: confirm from the evidence itself that a stop condition of your own current dispatch has been met, then apply that stop under that dispatch, as you would for any other evidence.
Only an addressed grant authorizes action; visibility and notifications do not.
For apparently misrouted work, ask its sender rather than acting.
Copy only seats whose next decision depends on the relay.
Set `HUMAN_GATE_REQUIRED` anew for the requested transition: yes only for a fresh operator decision, with its ask; otherwise no with a reason.
For delivery, use `relay connect --key <key-path>` with the literal run root; DD-v3-adapter § 4.1–4.2 governs the consequence: at most 20 TO relays ring at connect, then `relay connect --list` shows the remaining count; CC never rings.
If a relay cannot be read, report unavailable-file and do not act on it.
An arrival is E0; read the filed bytes before interpreting authority.

## Claim-evidence ladder

```text
E0 — hypothesis, suspicion, or unverified agent/sitrep claim.
E1 — file:line, diff hunk, schema, config, static search, or source proof.
E2 — local command proof: test, lint, typecheck, fixture, migration dry run, script.
E3 — integration/runtime proof: local stack, staging, worker, scheduler, DB/API check.
E4 — deployed/live proof: deployed SHA verified plus real target/tenant/environment workflow verified.
```

Choose evidence level by the strongest acceptance claim, not incidental commands.
A shell search of committed source is E1; executing the described behavior can supply E2 or E3.
Return an evidence-level disagreement rather than silently changing the target.
Do not say fixed, done, safe or verified without the evidence level.
Passing tests are E2; merge is not deploy and deploy is not live verification.

## 4-bucket audit verdict definitions

Name PRIMARY_BUCKET and fill all four buckets:

- still-open: requested behavior is absent or broken.
- already-closed: the behavior exists, including flagged, dead or alternate paths.
- product-overlapped: another owner or feature covers enough to narrow or reroute.
- recommended-next: implement, promote, reroute, defer, reject or ask the operator.

Do not contradict the recommendation; promoting existing code cannot be reported as no existing path.

## Hard escalation triggers

Escalate sensitive authorization or data, destructive changes, money or trust-critical state, downstream automation, async workers, shared contracts, materialized user controls, runtime-role mismatch, broad or ambiguous scope, residual risk or skipped live verification.
An index monotonic-from marker is also a hard escalation trigger.
Never self-insert a marker; the operator must ratify a stabilized historical inversion, never a live race, bad stamp, missing or duplicate rows.

## Operator-judgment categories

Route semantics, irreversible changes, unclear ownership, conflicting criteria, residual risk, failed-test merges, waivers and live-verification skips to the operator.
Route user-visible behavior and broad scope expansion to the operator.

## Duplicate/already-built audit gate

Check implemented, flagged, dead, alternate, tested and overlapping paths.
For existing paths, recommend promotion, wiring, tests, rerouting or closure.
Map every addressed audit finding once to an owned target, reasoned rejection, overlap owner or verified closure.
Name checked evidence or none with a reason; unmapped findings defeat reconciliation.
Keep the obligation ledger readable by other seats.

## Boundary contract fields

Use when work crosses domains/components/services or creates data for another consumer.

```text
Writes: <canonical object/event/state produced>
Reads: <upstream state/events consumed>
Target entity: <materialized object that must change>
Downstream consumer: <UI/worker/report/materializer/planner/etc.>
Contract: <fields, statuses, events, API/DB shape>
Proof: <E2/E3/E4 proof writer and reader meet>
No-consumer action: <reject | defer | follow-up | reroute>
```

Block writer-with-no-reader, reader-with-no-writer, dead controls, evidence-only updates that miss the target entity, and speculative builds with no consumer.

## Ceremony tiers and downgrade record

Use the lightest safe tier: tiny, small, medium, large or production-risk.
Complete an escalation scan before justifying a downgrade.
Scan authorization/data; destructive writes; money/trust; automation; workers/retries; shared contracts; controls/materializers; runtime-role mismatch; ambiguous scope, residual risk and live-verification gaps.
State yes, no or unknown with evidence for each scanned category and record the aggregate result.
Quote schedule or operator pressure before deciding whether a waiver exists.
If every row is no, explain why the downgrade is safe.
Any yes or unknown requires the heavier workflow unless the operator grants a waiver after seeing the completed scan.
Record the exact post-scan answer, the decision it covers and the accepted residual risk.
Earlier urgency or general permission is not a waiver; never self-waive or describe waived risk as safe.
Reversibility needs captured application state or executable restoration; vague backups and logs are insufficient.

## Rationalization counter-table

| Rationalization | Required counter |
|---|---|
| Urgency waives review. | Require the applicable post-scan waiver. |
| A tiny deviation is harmless. | Route it before editing. |
| Green tests authorize merge. | Require separate merge authority. |
| Visibility assigns work. | Apply your addressed scope. |
| History guarantees rollback. | Name executable restoration. |
| Silence proves truth. | Run the discriminating check. |

## SITREP content

Write SITREPs only for news the engine cannot know.
Name artifact, claims/evidence, actions with Git/disk refs, remaining work, blockers, scope risk and next action.
Check incoming claims against repo, PR, runtime or deployment.
Do not report known engine state merely to restate it.
For read-only reports, report fresh final Git status literally, or why unavailable.
Do not say no actions when the status or evidence shows edits.

## Fold scope

Before any fold edit, list every file to be touched and mark each in or out against the findings scope.
Any out row stops the fold: relay the deviation and wait before editing.
Scope precedes action even when an edit would ultimately prove in scope.
Put the scope and its result above the action references in the fold report.
Check row truth against locked scope and findings; valid structure proves no truth.

## Merge and live verification

Merge is a separate human/operator gate.
Use merge-blocked, merged-not-deployed, deployed-not-live-verified, failed-live-verification, complete or human-decision-required to state the actual disposition.
A review approval that leaves a merge condition open must say human-decision-required.
Do not grant on reviewer silence; re-address the request or route the missing response to the operator.
An operator answer covers current conditions, head, base and plan; changes need another answer.
Record chat authorization in the operator's own words; do not infer approval.
The quote records the decision; the acting seat still needs its filed grant.
Preserve failed or partial evidence and named exceptions; an accepted break does not become a green check.

## Tool pointers

Run `relay lint` on a draft before filing; `_cmd_lint` in `tools/relay_engine/cli.py` chooses the check, whose structural success does not establish claim truth.
Read `tools/TESTING.md` for the everyday checks instead of copying its command list into role instructions.
For relay field grammar use `parse` in `tools/relay_engine/header.py`; do not reproduce the field table in working instructions.
For filing paths use `_rendered_path` in `tools/relay_engine/ledger.py`; do not invent a receipt or rewrite a filed path.
