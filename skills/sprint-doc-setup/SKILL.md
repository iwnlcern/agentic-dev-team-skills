---
name: sprint-doc-setup
description: Use when setting up or maintaining the working document tree for a sprint at any tier — standalone pair, orchestrator team, or master. Requires Superpowers.
---

# Sprint Doc Setup

Use this skill to create or maintain the file substrate for one sprint at any tier. This skill owns **where sprint documents and relays live**, their naming conventions, and lint-clean relay formatting. It does not define relay semantics; use the role skills' adjacent `protocol.md` for authority, dispatch, merge, scope, and evidence rules.

## Mandatory prerequisites

**Superpowers is mandatory.** Use Superpowers planning/review procedures when the sprint doc setup is part of a larger plan. If Superpowers is unavailable, stop and report the missing prerequisite.

## Output tree

Every independently booted Orchestrator Pair with its own `RUN_ID` gets exactly one sprint document root, even when commissioned by a parent sprint; parent/child linkage is recorded by relay, never by sharing the parent's document root.

Create one active sprint directory:

```text
docs/sprints/active/YYYY-MM-DD-<topic>/
  ROADMAP.md
  RECONCILE.md
  audits/
  designs/
  plans/
  reviews/
  results/
  .relays/        # operational relay substrate; tracking choice is recorded in ROADMAP
```

Use repo-root-relative paths. The listed standard directories are a fixed vocabulary instantiated on first use; a standard directory never used by sprint close may be absent, and an empty or present standard directory is equally conforming. An additional top-level file or directory requires a one-line declaration in the run's ROADMAP.

Whether the relay substrate is tracked or gitignored is operator discretion with no fixed default. Declare the choice and record it in the run's ROADMAP. This repository's gitignored, public-facing selection is already recorded; do not infer that selection for another run.

`docs/sprints/active/` and `docs/sprints/archive/` are first-class; completing or parking a sprint moves its directory; the move is recorded by relay. Compatible with the declared-root marker: roots travel with the tree. A stale external path is resolved by the move relay.

## Relay tree convention

```text
.relays/<RUN_ID>/INDEX.md
.relays/<RUN_ID>/boot/<boot-dispatch-id>/<PHASE>-<ROLE>-<YYYYMMDD-HHMMSS>.md
.relays/<RUN_ID>/<cycle-id>/<PHASE>-<ROLE>-<YYYYMMDD-HHMMSS>.md
```

The relay root is literal `.relays` (a rule, not a default) anchored beside the sprint or lane documents it serves, with exactly one run level `.relays/<RUN_ID>/`. The effective root of a run is recorded once by the INDEX `root:` marker; a `RELAY_ROOT` override or operator-directed divergence is valid only when so recorded. A divergent directory without a recorded override is nonconformance, not a second compliant reading.

The `boot/` subtree exists only in orchestrator-tier (and above) runs, for orchestrator-planner `init` relays. Boot relays use the existing relay schema (`PHASE: SITREP`, `AUTHORITY: report-only`) and grant no work authority; they bring operator-relayed seats online.

Index topology follows team structure: a master team keeps one lifetime INDEX at its own relay root; an independent orchestrator team opens one INDEX per sprint run; a subservient orchestrator team keeps one local INDEX per run rooted at its own run directory.

Every new INDEX carries exactly one own-line `root: <path>` marker in its header before the first row, pre-filled by topology:

```text
independent .relays/<run>/INDEX.md declaring .relays/   -> root: ..
standalone pair .relays/<run>/INDEX.md declaring .relays/ -> root: ..
subservient run-local INDEX declaring its run directory -> root: .
master master/relays/INDEX.md declaring master/         -> root: ..
```

Create the INDEX and append the first boot row in one step; do not leave a newly seeded INDEX parked between those operations. Immediately before the first row, the expected state is a root marker plus the schema header with zero rows; native acceptance of that transient state is H26, the faults-owned half.

The canonical marker value is a POSIX-separator path relative to the directory containing the INDEX (`.` and `..` permitted). A tracked absolute marker is a reviewer-detected conformance violation; untracked machine-local indexes may use absolute values. An existing index opts in by appending its first and only marker at end-of-file. A second recognized marker is a duplicate refusal, never an update mechanism. An index without a marker receives no resolution semantics or resolution check — there is no repo-root default or implied base.

`INDEX.md` is append-oriented. The INDEX is exempt from relay-lint's per-relay checks; its own checks run in `--index` mode, which a per-file invocation must request explicitly. It uses exactly this schema:

```text
| time | phase | role | dispatch | parent | from | to | cc | status | file |
```

The `role` cell is the lowercase hyphenated role word from the address grammar; absent values are exactly `—`. The `status` cell is one kebab-case token with no spaces, parentheses, or sentences. Statuses use the three-way canonical / legacy-synonym / free partition and the full disposition table in the adjacent `protocol.md`: for the seven reserved meanings, new rows use `sent`, `dispatched`, `review-requested`, `approve`, `must-revise`, `returned`, or `complete`; a meaning outside those seven uses a free-tail token, while a legacy synonym is never emitted in a new row.

A `file` cell is either a path resolving under the declared root or the explicit literal `none — <short reason>`, which the resolution check skips by rule. The reason-bearing form is not an absence; bare `—` is not valid in the `file` cell.

Do not assume `.relays/` is shared across worktrees or sessions. If a receiver cannot access the path, relay the file contents verbatim or attach the file.

## Standalone pair runs

For a standalone pair run, the operator boots seats directly; do not create a `boot/` subtree. Operator quickstart: open one session per seat and load its role skill; boot the standalone Planner and Implementer sessions directly, or for an orchestrator-team run boot the Orchestrator Planner and trigger `init`. Carry each returned file-first hand-off pointer to its exact `TO` seat, and treat `CC` as context only. Use the same relay tree, naming conventions, and `INDEX.md` discipline otherwise. The artifacts are never optional; who writes them depends on the root's mode. In an engine-managed root (a daemon has cut over and serves it), seats file through `.engine/drafts/` + `relay submit` and only the daemon writes `INDEX.md`. In a hand-authored root (pre-cutover, or rolled back), the D7 ceremony floor still files relays, maintains `INDEX.md` under the gated-append rule below, and produces the full auditable trail.

## Header convention

Use the canonical header fields from the role skills' adjacent `protocol.md`:

```text
ROLE:
PHASE:
AUTHORITY:
DISPATCH_ID:
PARENT_DISPATCH_ID:
RUN_ID:
CEREMONY_TIER:
EVIDENCE_TARGET:
HUMAN_GATE_REQUIRED:
FROM:
TO:
CC:
SUBJECT:
```

`PARENT_DISPATCH_ID` is the work-lineage edge used by lineage gates. `IN_REPLY_TO` is local/display threading only and is not a gate input. Do not invent another threading scheme.

`SUBJECT` is local/display-only, placed after `CC`, and never a gate input.

Identity, location, and integrity are separate: `DESIGN_LOCK_ID` / `PLAN_LOCK_ID` carry a lock value — a logical identity or repo-relative path, optionally annotated ` @ sha256 <hex>` (equality on the unannotated value); optional `DESIGN_ARTIFACT` / `PLAN_ARTIFACT` fields carry filename-stem locators; optional `DESIGN_SHA256` / `PLAN_SHA256` fields carry lowercase-hex sha256 values for the referenced bytes. Beyond that annotation, never combine them. A present digest requires its paired locator under the routed fail-closed check; a locator without a digest is permitted. New artifact stems use `designs/DD-<cycle>-<YYYYMMDD>.md` and `plans/PL-<cycle>-<YYYYMMDD>.md`, with `-erratum-N`, `-supplement-N`, or `-amendment-N` suffixes for amendments and errata.

## Naming conventions

- `RUN_ID`: short dotted or hyphenated run id, e.g. `site-qi-2026-06-19`.
- `DISPATCH_ID`: authority-chain relays — PLAN, PLAN-REVIEW, IMPL, and the merge grant/claim pair — carry handoff-scoped ids, and each `PARENT_DISPATCH_ID` names its immediate predecessor; every other relay in the cycle carries the cycle id; one directory per cycle. PLAN, PLAN-REVIEW, and IMPL each use their own unique handoff id at the parent edge; a reissued PLAN or PLAN-REVIEW increments its numeric suffix (`-2`, `-3`, …) without asking the orchestrator; the merge grant/claim pair shares its merge-handoff id; non-authority-chain status relays carry the cycle id, never a handoff id. The directory is named by the cycle id; an authority-chain relay follows its parent lineage to that id and stays in the same directory rather than opening one named by its handoff id; the gated design-doc PLAN's parent edge names the approving DESIGN-REVIEW.
- Boot dispatch ids render as `<run>-boot-<owner>-<role>` with `<owner>` byte-equal to the address's owner segment; run-prefix stutter (`s1-boot-s1-core-planner`) is accepted. Spell the owner segment exactly once, the same way as in the address.
- Addresses: dotted lowercase owner-role form, e.g. `qi-a.planner`, `qi-a.implementer`, `site-qi.orchestrator-planner`.
- Timestamps: `YYYYMMDD-HHMMSS` in local sprint time unless the operator specifies UTC. Read the real clock at authoring time — never infer a stamp from a neighbouring relay or a tidy cadence. `relay-lint` fails an impossible or drifted stamp, and `relay-lint --index` fails an index whose `time` column decreases or disagrees with the filename it points at.

## Lifecycle mapping

```text
BOOT/init    -> .relays/<RUN_ID>/boot/<run>-boot-<owner>-<role>/... (report-only onboarding; no work authority)
AUDIT        -> .relays/<RUN_ID>/<cycle-id>/... (sole report of record); audits/<dispatch-id>.md only when explicitly commissioned, as a non-duplicating pointer to the immutable relay
DESIGN       -> designs/DD-<cycle>-<YYYYMMDD>[-<erratum|supplement|amendment>-N].md; DESIGN_LOCK_ID remains the separate logical identity
PLAN         -> plans/PL-<cycle>-<YYYYMMDD>[-<erratum|supplement|amendment>-N].md; PLAN_LOCK_ID remains the separate logical identity
PROCEED-TO-PLAN -> PHASE: PLAN, AUTHORITY: plan-only sequencing relay; proceeds are subject/body designations, not phases
PLAN-REVIEW  -> reviews/<dispatch-id>-plan-review.md
IMPL         -> relay dispatch + PR branch, not a tracked sprint doc by default
REVIEW-FOLD  -> reviews/<dispatch-id>-fold.md
MERGE-GATE   -> results/<dispatch-id>-merge-gate.md
LIVE-VERIFY  -> results/<dispatch-id>-live-verify.md
RECONCILE    -> RECONCILE.md, the sole tracked durable reconciliation projection per sprint root; PHASE: RECONCILE relays cite the exact section they append or state projection-pending
```

The AUDIT relay is the sole report of record. `audits/<dispatch-id>.md` is created only when the dispatch explicitly commissions a tracked publication, and is then a non-duplicating pointer carrying the immutable relay reference, never a second copy of findings.

`RECONCILE.md` is the only tracked durable reconciliation projection per sprint root. `PHASE: RECONCILE` relay files are transport records and must cite the exact `RECONCILE.md` section they append, or state `projection-pending`.

## Gotchas

- Flush-left relay headers are lintable; indented headers may become prose.
- Fenced examples of `DISPATCH IMPL` / `DISPATCH MERGE` are inert; operative tokens must be bare, unfenced, un-backticked, and alone on their own line in a valid relay addressed to the actor.
- `INDEX.md` is routing context, not an authority relay.
- Do not proxy-author relays for another seat. `FROM` is your own address.
- Before daemon activation, the relay author appends exactly its own row immediately after filing. Re-read the tail immediately before appending; append at end-of-file in write order. Never re-sort: an observed concurrency inversion is registered by a row or relay, never repaired by rewriting rows. This gated append narrows the concurrency race without closing it; only the serialized daemon writer owned by v29-engine closes it.
- After daemon activation, only the daemon appends, and the daemon assigns the row timestamp at serialized append; seats stop writing the file. Rollback to hand mode re-enters the preceding hand-written rule.
- A `monotonic-from` marker is never yours to insert: eligibility, the stamp-repair rule (rename the file and fix the index row), and the operator gate are defined in `protocol.md`'s index policy — route the request there rather than appending the line that makes your own red index green.
- Do not let this skill redefine relay semantics. It adopts the schema in the role skills' adjacent `protocol.md`.

## Deliverable

Return:

```text
Sprint doc root:
RUN_ID:
Created/updated files:
Relay root:
INDEX.md status:
Gitignore note:
Open operator decisions:
```
