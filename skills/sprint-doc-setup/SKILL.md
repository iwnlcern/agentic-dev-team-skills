---
name: sprint-doc-setup
description: Use when setting up or maintaining the working document tree for a sprint at any tier — standalone pair, orchestrator team, or master.
---

# Sprint Doc Setup

Use this skill to create or maintain the file substrate for one sprint at any tier. This skill owns **where sprint documents and relays live**, their naming conventions, and lint-clean relay formatting. It does not define relay semantics; use the role skills' adjacent `protocol.md` for authority, dispatch, merge, scope, and evidence rules.

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

`docs/sprints/active/` and `docs/sprints/archive/` are first-class; completing or parking a sprint moves its directory, and the move is recorded by relay.
A hand-authored root moves with its tree.
An engine-managed root does not: its engine record keeps the path it was registered at.
The daemon keeps serving a moved root, also after a restart, but four things break:

- root lint behaves as on an unmoved root while the daemon runs, because it reads the daemon's record, but with the daemon stopped, root lint finds no usable record (`read_lint_context` in `tools/relay_engine/daemon.py`), so it names no mismatch and silently re-judges every filed relay;
- a relay that would raise an engine notice is refused with `E-DAEMON-DOWN` while the daemon runs, because the notice opens the root at the recorded path (`notice_effect` in `tools/relay_engine/roster.py`);
- absolute key and draft paths that still name the old place are refused with `E-PATH-ESCAPE` (`_relative` in `tools/relay_engine/client.py`);
- watcher bindings keep the old root's path (`bind_from_receipt` in `tools/adapters/relay-monitor.py`).

A closed sprint loses nothing by this, since only the engine's own commands read the record; do not move a root whose work must continue.

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

`INDEX.md` is append-oriented.
Lint drafts with `relay lint <draft> --root <run-root>` before submission; `_cmd_lint` in `tools/relay_engine/cli.py` selects the check.
For legacy INDEX rules, consult `lint_relay_index` in `tools/relay_engine/rules.py`; never hand-edit a daemon projection.
It uses exactly this schema:

```text
| time | phase | role | dispatch | parent | from | to | cc | status | file |
```

`index_rows` in `tools/relay_engine/render.py` determines daemon-rendered `role` cells; absent values are exactly `—`.
The `status` cell is one kebab-case token with no spaces, parentheses, or sentences.
For legacy hand-authored indexes, `lint_relay_index` in `tools/relay_engine/rules.py` decides the canonical / legacy-synonym / free partition: for the seven reserved meanings, new rows use `sent`, `dispatched`, `review-requested`, `approve`, `must-revise`, `returned`, or `complete`; a meaning outside those seven uses a free-tail token, while a legacy synonym is never emitted in a new row.

A `file` cell is either a path resolving under the declared root or the explicit literal `none — <short reason>`, which the resolution check skips by rule. The reason-bearing form is not an absence; bare `—` is not valid in the `file` cell.

Do not assume `.relays/` is shared across worktrees or sessions. If a receiver cannot access the path, relay the file contents verbatim or attach the file.

## Standalone pair runs

For a standalone pair run, the operator boots seats directly; do not create a `boot/` subtree.
Operator quickstart: open one session per seat and load its role skill; boot the standalone Planner and Implementer sessions directly, or for an orchestrator-team run boot the Orchestrator Planner and trigger `init`.
For the v3 delivery contract, connect with `relay connect --key <key-path> --root <run-root>` (DD-v3-adapter § 4.1–4.2).
Only `TO` rings; carry the handoff pointer to seats using the fallback.
Use the same relay tree, naming conventions, and `INDEX.md` discipline otherwise.
The artifacts are never optional; who writes them depends on the root's mode.
In an engine-managed root (a daemon has cut over and serves it), seats file through `.engine/drafts/` + `relay submit` and only the daemon writes `INDEX.md`.
In a hand-authored root (pre-cutover, or rolled back), the D7 ceremony floor still files relays, maintains `INDEX.md` under the gated-append rule below, and produces the full auditable trail.
The root's mode is an OPERATOR CHOICE made before the first boot relay: at init (or before booting standalone seats), the booting seat asks the operator whether this run's root is engine-managed or hand-authored and records the answer in its first relay — never infer the mode from a document or a default.
For an engine-managed root, follow [Managed-root setup](#managed-root-setup) before the top Planner's hand-offs.
A root never changes mode implicitly (`relay migrate` is the only path between the modes).

## Managed-root setup

The operator chooses the root's mode before its first boot relay, as described in [Standalone pair runs](#standalone-pair-runs).
For the managed mode, follow this order:

1. The operator names the top seat for the root's first start; every later start names the top seat already on record.
   The eligible starter runs the start: the master Planner where a master tier exists, else the Orchestrator Planner, else the operator; in a standalone pair, the operator.
   The starter passes the designated seat, including itself if the operator named it, and never picks a top seat on its own.
   A first designation records the top-seat name; stand-down leaves that record in place.
   Starting with that recorded name reuses its existing key only while its occupancy remains active; after stand-down, startup issues a fresh occupancy and key under the same name.
   The earlier key file stays beside the new one after stand-down and restart; use the key file named for the `occupant_id` of the latest `occupied` entry shown by `tools/relay seat show <address>`, since the earlier key is refused with `E-KEY-MISMATCH`.
2. The top seat declares the roster with `seat declare` using its own key issued by that start: every seat the run will address, the reviewer included, and for a commissioned team its members on the receiving root.
   Check that each declaration result is `added` or `exists`; otherwise stop setup and report the result to the operator.
3. The top seat registers each downstream seat once, at first occupancy; that registration files its engine boot record and issues its key.
   Leave each key under the root's seat directory where the booted seat reads it.
   Register the `operator` seat whenever the operator will file go-aheads or overrides as relays; this is always required in a standalone managed pair, where the operator's merge grant must be filed.
4. Only then does the top Planner file its own hand-offs.

The top seat does not register separately.
A root without a top seat on record needs an operator-authorized start naming it; do not choose the first registrant as top seat and do not stop or restart a daemon without the operator.
A resumed or re-booted seat uses the key for its current occupancy; a booted seat never registers itself, because the top seat registered it.
A seat that cannot find its key under the root's seat directory asks the top seat or the operator for it.
A `seat-occupied` refusal means the seat is already registered, not that it needs replacement.
Replacing an occupant with `seat replace` is operator-directed only.
A roster declaration recognises names only: it occupies no seat, issues no key, grants no authority, delivers nothing across roots and follows no later changes in another root.
For the maintained start/register/submit procedure, see `tools/relay_engine/README.md` § Typical flow.
For roster declaration flags, which that flow does not cover, see `tools/relay seat declare --help`.
The two input forms not described there are defined in `tools/relay_engine/cli.py` (`_cmd_seat_declare`): `--key` takes the top seat's key file resolved against the root, as a root-relative path or an absolute path inside it, and `--from-file` takes one `<address> <role>` pair per line from a file resolved against the command’s working directory, not the relay root.

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

Place `SUBJECT` after `CC`.
For its required managed-admission checks, follow `parse_draft` in `tools/relay_engine/envelope.py`; it is not an authority-check grant condition.

For the pair's design lineage, `_lineage` in `tools/relay_engine/authority.py` compares the declared design identities directly; keep them identical across the lineage.
Use the separate artifact and digest declarations checked by `lock_digest_shape_errors` in `tools/relay_engine/rules.py` to name and pin artifact bytes.
For lock annotations, follow `_declarations` in `tools/relay_engine/authority.py` and `xroot_plan_gate` in `tools/relay_engine/rules.py` for the applicable local or foreign path.
New artifact stems use `designs/DD-<cycle>-<YYYYMMDD>.md` and `plans/PL-<cycle>-<YYYYMMDD>.md`, with `-erratum-N`, `-supplement-N`, or `-amendment-N` suffixes for amendments and errata.

## Naming conventions

- `RUN_ID`: short dotted or hyphenated run id, e.g. `site-qi-2026-06-19`.
- `DISPATCH_ID`: authority-chain relays — PLAN, PLAN-REVIEW, IMPL, and the merge grant/claim pair — carry handoff-scoped ids, and each `PARENT_DISPATCH_ID` names its immediate predecessor; every other relay in the cycle carries the cycle id; one directory per cycle. PLAN, PLAN-REVIEW, and IMPL each use their own unique handoff id at the parent edge; a reissued PLAN or PLAN-REVIEW increments its numeric suffix (`-2`, `-3`, …) without asking the orchestrator; the merge grant/claim pair shares its merge-handoff id; non-authority-chain status relays carry the cycle id, never a handoff id.
  For a hand-authored root, keep an authority-chain relay in its cycle directory by following its parent lineage.
  For an engine filing, use the receipt's path; `_dispatch_lane` and `_rendered_path` in `tools/relay_engine/ledger.py` determine that path.
  The gated design-doc PLAN's parent edge names the approving DESIGN-REVIEW.
- Boot dispatch ids render as `<run>-boot-<owner>-<role>` with `<owner>` byte-equal to the address's owner segment; run-prefix stutter (`s1-boot-s1-core-planner`) is accepted. Spell the owner segment exactly once, the same way as in the address.
- Addresses: dotted lowercase owner-role form, e.g. `qi-a.planner`, `qi-a.implementer`, `site-qi.orchestrator-planner`.
- Timestamps: `YYYYMMDD-HHMMSS` in local sprint time unless the operator specifies UTC.
  Read the real clock at authoring time — never infer a stamp from a neighbouring relay or a tidy cadence.
  Use `relay lint` on the draft before filing; do not edit a daemon-assigned stamp or INDEX row.

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
