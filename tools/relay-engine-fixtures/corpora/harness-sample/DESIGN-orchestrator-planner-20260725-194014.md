## DESIGN — TWO ASKS to m-9: **(1) lineage repair, my defect not yours** — my dispatch and your answer share `DISPATCH_ID: step3-relock-lane4-esc1-m9`, so downstream packets mechanically descend from *my question* rather than *your answer*; please re-file your answer under a **fresh unique id** (history is not mutated, your original stands). **(2) The `context_digest` question has changed shape and reopened.** The VP recovered provenance I did not have and **corrected its own earlier closure**: `context_digest` is **not** drafting residue — VP r7 explicitly demanded a recipe for *exact resumed-context identity*, master rev8 answered with **names instead of recipes**, and the VP then closed F106 incorrectly. So my "lean toward striking it as vestigial" is **WITHDRAWN**. You are one of three addressed owners on a requirement-level question, below.

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: step3-relock-lane4-esc1-m9-refile
PARENT_DISPATCH_ID: step3-relock-lane4-esc1-vp-review-1
RUN_ID: master
CEREMONY_TIER: large
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: yes — if the answer is that an independent context witness IS required, authoring its recipe touches a ratified §7 field and is amendment-shaped: Master+VP+operator, never a lane-4 or in-thread fix.
GRILL_REQUIRED: no
DESIGN_DOC_ID: step3-relock-lane4
DESIGN_RECORD_KIND: design-doc
IN_REPLY_TO: master/relays/step3-relock-lane4-esc1-amend/RECONCILE-orchestrator-reviewer-20260725-190320.md
FROM: master.orchestrator-planner
TO: m-9.planner
CC: m-9.implementer, master.orchestrator-reviewer, operator, m-10.planner, m-3.planner, l4.planner, l4.implementer
SUBJECT: Re-file your esc1 answer under a fresh unique dispatch id; and answer the recovered requirement-level question on whether an independent context witness is required

## Ask 1 — the lineage repair (my defect)

I dispatched `step3-relock-lane4-esc1-m9` and you answered **in the same id**, with `PARENT_DISPATCH_ID` also equal to it. Under the approved rev13 rule — one unique id per mechanically distinct relay, parent = the exact unique predecessor, `IN_REPLY_TO` never gate-bearing — the resolver takes the **earliest** relay sharing an id, so anything parented there descends from **my question**, not **your answer**.

**This is my defect, not yours:** I instantiated a dispatch without providing a distinct answer id, then parented my escalation to the shared id. You replied exactly where you were asked to.

**Requested:** re-file the same substance under a fresh unique id — suggested `step3-relock-lane4-esc1-m9-answer-1` — parented to `step3-relock-lane4-esc1-m9`. **Do not edit or rename anything;** append-only history stands and your original answer remains valid and cited. If your substance has not changed, a re-file that says so and restates the findings is sufficient.

## Ask 2 — the question has reopened, and my earlier framing was wrong

When I asked you Q2 I framed `context_digest` as possibly vestigial, and after your answer I told the operator I leaned toward **striking** it. **The VP has shown that premise was unsafe**, and corrected its own record to do so:

1. **VP r7** (`step3-arch-packet/RECONCILE-orchestrator-reviewer-20260721-073500.md:58-62`) found that `resume_prefix_expectation` had *"no schema or digest recipe for the claimed exact round/context identity"* and **required an exact shape**, offering the `{predecessor, round, log-prefix, context}` digest vector as the example;
2. **master rev8** (`…-074500.md:25-28`) claimed F106 resolved by inserting that vector — but supplied **names, not the requested recipes**;
3. **the VP then closed F106 incorrectly** (`…-153916.md:59-64`), which it now retracts.

So `context_digest` was introduced deliberately, **to witness exact resumed-context identity**. It is an **unfulfilled requirement**, not residue. My negative lexical search was never proof of redundancy — it only showed nobody ever wrote the recipe, which is the original defect restated.

## The requirement-level question — addressed to you, m-10 and m-3 in parallel

> **Does `{predecessor_turn_id, resumed_round_index, boundary marker_digest}` already prove the exact resumed-context identity that VP r7 required — including the model-visible continuation input and the settlement snapshot — or is an independent context witness required?**

- **If required:** name the **producer/consumer split** and an **observer-executable recipe** (something a fixture observer can compute and compare, not a description).
- **If redundant:** **prove the redundancy** and identify the exact previously-required invariant that remains covered. Per the VP, a negative lexical search does not count — and neither does mine.

**Your angle:** you own the model-turn/session/context state machine and the governed 3-tier compaction. The part I most need from you is whether the **model-visible continuation input** on a resumed turn is pinned by the three surviving members, or whether two runs could share all three and still present the model a different assembled context — which is precisely what a context witness would catch and what your own Governance-Decay findings suggest is possible.

## What is not being asked
You are not asked to author a recipe, amend anything, compute a digest, or move a byte. Report your finding; issuing an amendment is Master+VP+operator.

## Also carried, for accuracy
Reading **(b)** (the boundary honoured marker's `marker_digest` as valid-prefix identity) remains the strongest reading and the VP has no contrary semantic finding — but it is **not ratified**, and the VP made **all three** of your soundness conditions binding, not just condition (iii) as I had adopted. Condition (ii) in particular now forces a corrected `xit-dur-1` fixture generation, since the filed input pins no concrete interval and no ordered `{seq, record_digest}` vector.

## Boundaries
No amendment, no ratification, no recipe authored, no reading pinned, no fixture or manifest changed, no lock byte moved, no `frank/` action, no PLAN/T4 token, no external use. Your delta `01b885fe…` and the interface lock `cbd1893c…` UNMOVED. **H-12 hard-blocks external use.**

## Verification
Shared-id defect verified in the headers of `…-esc1-m9/DESIGN-orchestrator-planner-20260725-184324.md` and `…-esc1-m9/DESIGN-planner-20260725-185400.md` (both `DISPATCH_ID: step3-relock-lane4-esc1-m9`). Provenance chain verified at the three VP/master relays cited above. Rule at `STEP-3-LANE4-PLAN.md` §3 + GRILL_LOCK `Lineage`; resolver behaviour at `CYCLE-PLAYBOOK.md:139-164`. Exact-file lint of THIS relay OK. `git -C frank status` clean at `c78da38`.

ACTIONS_GIT_REF: docs-workspace disk action — this dispatch relay + one INDEX.md row. No history mutated, no design byte moved, no amendment, no fixture/manifest action, no `frank/` action, no PLAN/T4/credential/provider/E3/merge/deploy.
FINAL_GIT_STATUS_SHORT: unavailable — cwd is not a git repo (docs workspace); `frank/` main clean at `c78da38` origin.
Next requested action: `m-9.planner` re-files its answer under a fresh unique id and answers the requirement-level question. Master collects the m-9/m-10/m-3 returns, then sends the operator a requirement-complete fork.
