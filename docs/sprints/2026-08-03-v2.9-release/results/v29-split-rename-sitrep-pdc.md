# S7 deployment rename SITREP template — pdc

TEMPLATE_STATE: report-only, not applied
FROM: operator
TO: master.orchestrator-planner
PHASE: SITREP
DEPLOYMENT: pdc
AUTHORITY_GRANTED: none
DEPLOYMENT_CHANGE_MADE: none
OWNER_RENAME_DECISION: not made; proposal only

This is a report-only template for the operator to relay to pdc's top session. The top
session distributes the information down the deployment tree. This SITREP grants no
authority, is not an implementation or deployment instruction, and does not report that
pdc has applied any rename. Any later execution needs separate operator authority.

## Evidence-grounded pdc shapes

The migration applies by seat shape, not by treating this historical evidence as a
live-roster assertion:

- the pdc roadmap names domain owners `m-1`, `m-2`, and `m-3`, and its reconciliation
  ledger also records `m-4`;
- the ledger records a top `master.orchestrator-planner` / `master.orchestrator-reviewer`
  pair;
- the ledger records sub-team apex owner shapes such as `s2` and `s3`, including the
  live-at-that-record `s3.orchestrator-planner` and its reviewer;
- pdc's observed sub-team pair-owner shapes include `s2-core`, `s2-harness`, `s3-pack`,
  and `s3-open`.

Evidence sources (read-only):

- `/Users/jack/Programming/pdc/ROADMAP.md`
- `/Users/jack/Programming/pdc/master/RECONCILE.md`

Before any separately authorized rename, the top session must inventory which of these
or later seats are actually live. Historical names in those files are evidence of shapes,
not instructions to revive or rename retired sessions.

## Complete mechanical seat rules

Apply the first matching row to each live seat:

| Seat shape | Legacy address | New address |
|---|---|---|
| Master apex planner | `master.orchestrator-planner` | mechanically `master.master-planner` |
| Master apex reviewer | `master.orchestrator-reviewer` | mechanically `master.master-reviewer` |
| Domain planner | `m-N.planner` | `m-N.domain-planner` |
| Domain implementer/reviewer seat | `m-N.implementer` | `m-N.domain-reviewer` |
| Sub-team apex planner | `sN.orchestrator-planner` | `sN.orchestrator-planner` (already the new role word) |
| Sub-team apex reviewer | `sN.orchestrator-reviewer` | `sN.orchestrator-reviewer` (already the new role word) |
| Pair planner, including a sub-team pair | `<pair-owner>.planner` | `<pair-owner>.pair-planner` |
| Pair implementer, including a sub-team pair | `<pair-owner>.implementer` | `<pair-owner>.pair-implementer` |

For pdc, `<pair-owner>` includes sub-team pair owners such as `s2-core`, `s2-harness`,
`s3-pack`, and `s3-open`; a later pdc pair owner follows the same rule. Thus, for example,
`s3-pack.planner` becomes `s3-pack.pair-planner`, while the sub-team apex
`s3.orchestrator-planner` keeps its already-correct role word. Do not transform relay
directory names, dispatch IDs, branch names, or incidental text that merely contains an
owner-like string.

## Apex owner proposal — deployment choice, not acceptance

Keeping the current owner `master` while changing the role word mechanically produces the
documented stutter `master.master-planner` (and, symmetrically,
`master.master-reviewer`). That result is valid as the mechanical mapping.

Alongside it, this template proposes that pdc may choose to rename the apex owner from
`master` to `pdc`, yielding `pdc.master-planner` and `pdc.master-reviewer`. This is only a
proposal. pdc has not accepted it, and this template does not choose it. The deployment
must explicitly choose either the mechanical `master.*` owner result or an accepted owner
rename before its apex agents act.

Under D3, agents rename themselves: after separate operator authority and a deployment
choice, each live agent changes its own seat identity and reports the result upward. No
top session or peer silently rewrites another agent's identity.

## Per-host skill-pointer mapping

For each live session, replace the legacy skill pointer it loads with the role-word target
for its new tier. The source is always the generated plugin tree, never canonical
`skills/`. Materialize the same mapping independently on every host where that session can
run.

| Seat use | Legacy dir in `~/.claude/skills` | Role-word dir in `~/.claude/skills` | Generated source |
|---|---|---|---|
| Master apex planner | `orchestrator-planner` | `master-planner` | `plugins/adt-master/skills/master-planner` |
| Master apex reviewer | `orchestrator-reviewer` | `master-reviewer` | `plugins/adt-master/skills/master-reviewer` |
| Domain planner | `agent-pair-planner` | `domain-planner` | `plugins/adt-master/skills/domain-planner` |
| Domain reviewer | `agent-pair-implementer` | `domain-reviewer` | `plugins/adt-master/skills/domain-reviewer` |
| Sub-team apex planner | `orchestrator-planner` | `orchestrator-planner` | `plugins/adt-orchestrator/skills/orchestrator-planner` |
| Sub-team apex reviewer | `orchestrator-reviewer` | `orchestrator-reviewer` | `plugins/adt-orchestrator/skills/orchestrator-reviewer` |
| Pair planner | `agent-pair-planner` | `pair-planner` | `plugins/adt-pair/skills/pair-planner` |
| Pair implementer | `agent-pair-implementer` | `pair-implementer` | `plugins/adt-pair/skills/pair-implementer` |

| Seat use | Legacy dir in `~/.codex/skills` | Role-word dir in `~/.codex/skills` | Generated source |
|---|---|---|---|
| Master apex planner | `orchestrator-planner` | `master-planner` | `plugins/adt-master/skills/master-planner` |
| Master apex reviewer | `orchestrator-reviewer` | `master-reviewer` | `plugins/adt-master/skills/master-reviewer` |
| Domain planner | `agent-pair-planner` | `domain-planner` | `plugins/adt-master/skills/domain-planner` |
| Domain reviewer | `agent-pair-implementer` | `domain-reviewer` | `plugins/adt-master/skills/domain-reviewer` |
| Sub-team apex planner | `orchestrator-planner` | `orchestrator-planner` | `plugins/adt-orchestrator/skills/orchestrator-planner` |
| Sub-team apex reviewer | `orchestrator-reviewer` | `orchestrator-reviewer` | `plugins/adt-orchestrator/skills/orchestrator-reviewer` |
| Pair planner | `agent-pair-planner` | `pair-planner` | `plugins/adt-pair/skills/pair-planner` |
| Pair implementer | `agent-pair-implementer` | `pair-implementer` | `plugins/adt-pair/skills/pair-implementer` |

An old directory name can map to different role words for different tiers. Create the
new role-word pointer required by each session; do not globally repoint a shared legacy
alias in a way that changes another session's role. Verify each new pointer resolves to
the listed `plugins/adt-<tier>/skills/<role-word>` source before that session adopts it.

## Compatibility and history rule

- Permanent legacy readability: classifiers and readers must continue to accept existing
  legacy role/address forms, including bare `.planner` / `.implementer` and historical
  `.orchestrator-planner` / `.orchestrator-reviewer` uses at other tiers.
- Going forward: after a seat has completed a separately authorized rename, every newly
  authored relay uses its new tier-specific role word.
- Immutable history: do not rename, edit, regenerate, or rewrite historical relays,
  indexes, ledgers, reconciliation records, commits, or archived session records. Old
  bytes remain old bytes and remain readable.

## Report-back shape after a later authorized migration

Each agent's later report should identify its old address, its chosen new address, the
Claude and/or Codex role-word pointer it verified, and the unchanged-history check. Until
those reports exist and are reconciled, the honest state remains: template delivered,
rename not proven applied, and apex owner proposal not accepted.
