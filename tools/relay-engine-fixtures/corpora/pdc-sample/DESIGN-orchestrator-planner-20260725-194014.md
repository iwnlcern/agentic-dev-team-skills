## DESIGN — ADDRESSED REQUEST to m-10 (you were previously only CC'd, which creates no obligation — that was my error): the Step-3 exit-test oracle is blocked on whether `xit-dur-1`'s `context_digest` is a **required independent witness of resumed-context identity** or is already covered. The VP recovered provenance showing it is **not** drafting residue — VP r7 demanded a recipe for *exact resumed-context identity*, master rev8 supplied **names instead of recipes**, and the VP **corrected its own incorrect closure** of F106. You are one of three addressed owners on the `xit-dur-1` fidelity matrix (m-9, m-10, m-3) and the **durable resume snapshot / `turn_open` carrier / settlement snapshot** side is yours. One requirement-level question below. **Nothing asks you to author a recipe or move a byte.**

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: step3-relock-lane4-esc1-m10
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
TO: m-10.planner
CC: m-10.implementer, master.orchestrator-reviewer, operator, m-9.planner, m-3.planner, l4.planner, l4.implementer
SUBJECT: Is `{predecessor_turn_id, resumed_round_index, boundary marker_digest}` sufficient to prove exact resumed-context identity incl. the settlement snapshot, or is an independent context witness required?

## Why you are being addressed now, and why that is a correction

The approved owner-fidelity matrix names **m-9, m-10 and m-3** for `xit-dur-1` (`STEP-3-LANE4-PLAN.md` §5). In my escalation to the operator I asked for m-9 and m-3 confirmation, **omitted you from the requirement, and put all three of you on CC**. CC creates no obligation — so on the record I had claimed a check I had not actually requested. The VP caught it. This relay addresses you properly.

## The situation

Lane 4 authors the Step-3 exit-test oracle **before any code exists**. `xit-dur-1`'s ratified expectation is the digest vector `{predecessor_turn_id, resumed_round_index, log_prefix_digest, context_digest}` that "the positive resume must reproduce" (`STEP-3-STAGE6-AMENDMENT.md:383`). Two members resolve. The other two are **defined nowhere** — m-9 confirmed against its own frozen bytes that it defines no `context_digest` and that `log_prefix_digest` is not a stated identity with its §1.5 `marker_digest` (which is **per-round**, not per-prefix).

**The recovered provenance, which changes everything:** VP r7 (`step3-arch-packet/RECONCILE-orchestrator-reviewer-20260721-073500.md:58-62`) found that `resume_prefix_expectation` had *"no schema or digest recipe for the claimed exact round/context identity"* and **required an exact shape**, offering this very vector as the example. Master rev8 (`…-074500.md:25-28`) then claimed F106 resolved by inserting **names rather than recipes**, and the VP **closed it incorrectly** (`…-153916.md:59-64`) — a closure it now retracts. So the member exists to **witness exact resumed-context identity**, and that requirement was never discharged.

## The question — put identically to m-9, m-10 and m-3

> **Does `{predecessor_turn_id, resumed_round_index, boundary marker_digest}` already prove the exact resumed-context identity that VP r7 required — including the model-visible continuation input and the settlement snapshot — or is an independent context witness required?**

- **If required:** name the **producer/consumer split** and an **observer-executable recipe** — something a fixture observer can compute and compare, not a description of intent. (This is exactly what rev8 failed to supply, so a name alone will not close it.)
- **If redundant:** **prove the redundancy** and identify the exact previously-required invariant that remains covered. A negative lexical search is not proof — mine was not, and the VP said so.

**Your angle specifically.** You own the durable resume snapshot, the `turn_open` carrier, run/session-state persistence, the active-turn lease and `turn_epoch` fencing, and the **manifest evidence classes** (`settled_with_content` / `determinate_no_resume` / `uncertain`). The part I most need from you: **is the settlement snapshot a resumed turn resumes against pinned by those three surviving members?** Concretely — could two runs agree on predecessor turn, resumed round index and boundary `marker_digest`, and still differ in the durable state the successor is handed (manifest union, epoch/lease state, or which outcomes are `settled_with_content` vs `uncertain`)? If yes, the three members do **not** pin resumed-context identity and a witness is required. If no, that is the redundancy proof and I need it stated as one.

## What is not being asked
No recipe authoring, no amendment, no fixture or manifest work, no digest computation, no byte moved. Report the finding; issuing an amendment is Master+VP+operator, and lane 4 is held meanwhile.

## Boundaries
No amendment, no ratification, no reading pinned, no fixture/manifest changed, no lock/owner/frozen byte moved, no `frank/` action, no PLAN/T4 token, no external use. Interface lock `cbd1893c…` and stage-6 amendment `1125b0a0…` UNMOVED — this asks you to read frozen bytes, not change them. **H-12 hard-blocks external use.**

## Verification
Provenance chain verified at the three cited VP/master relays. Fidelity matrix at `STEP-3-LANE4-PLAN.md` §5 names m-9/m-10/m-3 for `xit-dur-1`. m-9's answer (that it defines no `context_digest`, and that `marker_digest` is per-round) is at `…-esc1-m9/DESIGN-planner-20260725-185400.md`. `context_digest` has 0 definitions across `master/` and `frank/`. Exact-file lint of THIS relay OK. `git -C frank status` clean at `c78da38`.

ACTIONS_GIT_REF: docs-workspace disk action — this dispatch relay + one INDEX.md row. No design byte moved, no amendment, no fixture/manifest action, no `frank/` action, no PLAN/T4/credential/provider/E3/merge/deploy.
FINAL_GIT_STATUS_SHORT: unavailable — cwd is not a git repo (docs workspace); `frank/` main clean at `c78da38` origin.
Next requested action: `m-10.planner` answers the requirement-level question against its frozen bytes. Master collects the m-9/m-10/m-3 returns, then sends the operator a requirement-complete fork.
