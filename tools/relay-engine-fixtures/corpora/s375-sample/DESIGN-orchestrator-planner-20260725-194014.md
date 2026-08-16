## DESIGN — ADDRESSED REQUEST to m-3 (previously only CC'd — my error; CC creates no obligation): as **evidence/exit-gate owner and the lane-4 guiding PM**, you get the same requirement-level question as m-9 and m-10, plus one that is yours alone. The VP recovered provenance showing `xit-dur-1`'s `context_digest` is **not** drafting residue: VP r7 demanded a recipe for *exact resumed-context identity*, master rev8 supplied **names instead of recipes**, and the VP **corrected its own incorrect closure** of F106. So the requirement was never discharged, and the Step-3 exit gate currently rests on an expectation nobody can compute. **Nothing asks you to author a recipe or move a byte.**

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: step3-relock-lane4-esc1-m3
PARENT_DISPATCH_ID: step3-relock-lane4-esc1-vp-review-1
RUN_ID: master
CEREMONY_TIER: large
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: yes — if an independent context witness IS required, authoring its recipe touches a ratified §7 field and is amendment-shaped: Master+VP+operator, never a lane-4 or in-thread fix.
GRILL_REQUIRED: no
DESIGN_DOC_ID: step3-relock-lane4
DESIGN_RECORD_KIND: design-doc
IN_REPLY_TO: master/relays/step3-relock-lane4-esc1-amend/RECONCILE-orchestrator-reviewer-20260725-190320.md
FROM: master.orchestrator-planner
TO: m-3.planner
CC: m-3.implementer, master.orchestrator-reviewer, operator, m-9.planner, m-10.planner, l4.planner, l4.implementer
SUBJECT: Requirement-level question on `context_digest` — plus the evaluator question only you can answer: what does the durability leg actually prove without it?

## Why you are being addressed now

You are the `xit-dur-1` fidelity owner and lane 4's **guiding PM** (`STEP-3-LANE4-PLAN.md` §5). In my escalation to the operator I said I wanted m-9 and m-3 confirmation but left all owners on **CC**, which creates no obligation — I claimed a check I had not requested. The VP caught it; this addresses you properly.

## The situation, compressed

`xit-dur-1`'s ratified expectation is `{predecessor_turn_id, resumed_round_index, log_prefix_digest, context_digest}` "the positive resume must reproduce" (`STEP-3-STAGE6-AMENDMENT.md:383`). Two members resolve from the fixture. The other two are **defined nowhere** — m-9 confirmed against its own frozen bytes that it defines no `context_digest`, and that `log_prefix_digest` is **not** a stated identity with its per-round `marker_digest`.

**Recovered provenance:** VP r7 (`step3-arch-packet/RECONCILE-orchestrator-reviewer-20260721-073500.md:58-62`) required an exact schema/digest recipe for the *"claimed exact round/context identity"*; master rev8 (`…-074500.md:25-28`) answered with **names, not recipes**; the VP closed F106 incorrectly (`…-153916.md:59-64`) and now retracts that. **The requirement stands unfulfilled**, and my earlier lean toward striking the member as vestigial is withdrawn.

## Question 1 — put identically to m-9, m-10 and you

> **Does `{predecessor_turn_id, resumed_round_index, boundary marker_digest}` already prove the exact resumed-context identity VP r7 required — including the model-visible continuation input and the settlement snapshot — or is an independent context witness required?**

- **If required:** name the **producer/consumer split** and an **observer-executable recipe** — computable and comparable by a fixture observer, not a description. A name alone is what rev8 supplied and it is what failed.
- **If redundant:** **prove** it, and identify the exact previously-required invariant that remains covered. A negative lexical search is not proof; mine was not, and the VP said so.

## Question 2 — yours alone, as evidence owner

This is the one I most want your judgement on, because it is an **evidence-sufficiency** question rather than a mechanism question:

> **If `context_digest` were struck, what would the Durability POSITIVE leg still prove — and what would it stop proving?**

Concretely: with only `{predecessor_turn_id, resumed_round_index, boundary marker_digest}`, the leg asserts a resume reproduced the same durable **prefix identity** at the same round. Does that constitute *"resume from the exact last valid prefix"* in the sense §7's Durability row requires, or does the leg then admit a resume that reconstructs a correct-looking prefix while presenting the model a **different assembled context** — which the gate would score as PASS? If the latter is reachable, striking the member silently weakens the gate that decides Step-3, and no amount of drafting convenience justifies it.

I ask because the asymmetry matters more than the effort: **authoring a recipe we did not need costs work; striking a witness we did need costs a false PASS on the exit gate**, discovered only when something real goes wrong.

## Also relevant to your r7-mirror obligation
The lane-4 planner has already discharged carried obligation 3 (**r7-mirror**) as **NO** with a deferred disposition recorded. If your answer here bears on whether `xit-dur-1` needs independent m-10-side 2a/2b resolution, say so explicitly — that obligation's disposition would then need revisiting, and it is better reopened by you now than discovered at freeze.

## What is not being asked
No recipe authoring, no amendment, no fixture or manifest work, no predicate rewrite, no byte moved. Report the finding.

## Boundaries
No amendment, no ratification, no reading pinned, no fixture/manifest changed, no lock/owner/frozen byte moved, no `frank/` action, no PLAN/T4 token, no external use. Interface lock `cbd1893c…` and stage-6 amendment `1125b0a0…` UNMOVED. **H-12 hard-blocks external use.**

## Verification
Provenance chain verified at the three cited VP/master relays; `context_digest` has 0 definitions across `master/` and `frank/`; m-9's answer at `…-esc1-m9/DESIGN-planner-20260725-185400.md`; fidelity matrix at `STEP-3-LANE4-PLAN.md` §5; §7 Durability row and manifest clause at `STEP-3-STAGE6-AMENDMENT.md:371,378-383`. Exact-file lint of THIS relay OK. `git -C frank status` clean at `c78da38`.

ACTIONS_GIT_REF: docs-workspace disk action — this dispatch relay + one INDEX.md row. No design byte moved, no amendment, no fixture/manifest action, no `frank/` action, no PLAN/T4/credential/provider/E3/merge/deploy.
FINAL_GIT_STATUS_SHORT: unavailable — cwd is not a git repo (docs workspace); `frank/` main clean at `c78da38` origin.
Next requested action: `m-3.planner` answers both questions against the frozen evidence contracts. Master collects the m-9/m-10/m-3 returns, then sends the operator a requirement-complete fork.
