# S7 deployment rename SITREP template — frank

TEMPLATE_STATE: report-only, not applied
FROM: operator
TO: master.orchestrator-planner
PHASE: SITREP
DEPLOYMENT: frank
AUTHORITY_GRANTED: none
DEPLOYMENT_CHANGE_MADE: none
OWNER_RENAME_DECISION: not made; proposal only

This is a report-only template for the operator to relay to frank's top session. The top
session distributes the information down the deployment tree. This SITREP grants no
authority, is not an implementation or deployment instruction, and does not report that
frank has applied any rename. Any later execution needs a later relay with addressed
operator authority. Every action-shaped statement below is conditional on that later
authority and a recorded deployment choice; until both exist, the mappings are
descriptive and non-actionable.

## Evidence-grounded frank shapes

The migration applies by seat shape, not by treating this historical evidence as a
live-roster assertion:

- frank's roadmap and reconciliation ledger record domain owners `m-1` through `m-10`;
- the ledger records a top `master.orchestrator-planner` / `master.orchestrator-reviewer`
  pair;
- frank records nested slice/sub-team apex seats, for example
  `s1.orchestrator-reviewer` and `s2.orchestrator-planner`, with pair owners such as
  `s1-core`, `s2-core`, and `s4-wire` below slice teams;
- frank also records direct pair-shaped slice owners such as `s8`, `s9`, and `s11` using
  bare `.planner` / `.implementer` suffixes. Those are pair-tier seats, not domain seats
  merely because their owner lacks a hyphenated child name.

Evidence sources (read-only):

- `<target-repo>/ROADMAP.md`
- `<target-repo>/master/RECONCILE.md`

If a rename is later separately authorized, its first step is for the top session to
inventory which of these or later seats are actually live. Historical names in those
files are evidence of shapes, not instructions to revive or rename retired sessions.

## Conditional mechanical seat rules

After later addressed operator authority and the deployment's roster/owner decisions,
the authorized migration applies the first matching row to each live seat:

| Seat shape | Legacy address | New address |
|---|---|---|
| Master apex planner | `master.orchestrator-planner` | mechanically `master.master-planner` |
| Master apex reviewer | `master.orchestrator-reviewer` | mechanically `master.master-reviewer` |
| Domain planner | `m-N.planner` | `m-N.domain-planner` |
| Domain implementer/reviewer seat | `m-N.implementer` | `m-N.domain-reviewer` |
| Sub-team apex planner | `sN.orchestrator-planner` | `sN.orchestrator-planner` (already the new role word) |
| Sub-team apex reviewer | `sN.orchestrator-reviewer` | `sN.orchestrator-reviewer` (already the new role word) |
| Legacy third reviewer seat | `sN.reviewer` | retired in place (not renamed) |
| Pair planner, including a sub-team or direct slice pair | `<pair-owner>.planner` | `<pair-owner>.pair-planner` |
| Pair implementer, including a sub-team or direct slice pair | `<pair-owner>.implementer` | `<pair-owner>.pair-implementer` |

For frank, `<pair-owner>` includes nested pair owners such as `s1-core`, `s2-core`, and
`s4-wire`, and direct slice-pair owners such as `s8`, `s9`, and `s11`. Thus, for example,
`s4-wire.implementer` would become `s4-wire.pair-implementer` and `s9.planner` would become
`s9.pair-planner`, while a true sub-team apex such as `s2.orchestrator-planner` keeps its
already-correct role word. This direct-slice-pair case is part of frank's observed shape
and remains distinct from pdc's map. Relay directory names, dispatch IDs, branch names,
and incidental text that merely contains an owner-like string remain outside any later
migration authority.

## Apex owner proposal — deployment choice, not acceptance

Keeping the current owner `master` while changing the role word mechanically produces the
documented stutter `master.master-planner` (and, symmetrically,
`master.master-reviewer`). That result is valid as the mechanical mapping.

Alongside it, this template proposes that frank may choose to rename the apex owner from
`master` to `frank`, yielding `frank.master-planner` and `frank.master-reviewer`. This is
only a proposal. frank has not accepted it, and this template does not choose it. If
migration is later authorized, the deployment records an explicit choice between the
mechanical `master.*` owner result and an accepted owner rename before its apex agents act.

Under D3, agents rename themselves: after separate operator authority and a deployment
choice, each live agent changes its own seat identity and reports the result upward. No
top session or peer silently rewrites another agent's identity.

## Per-host skill-pointer mapping

After later addressed operator authority, each live session replaces the legacy skill
pointer it loads with the role-word target for its new tier. The source is always the
generated plugin tree, never canonical `skills/`. The authorized migration materializes
the same mapping independently on every host where that session can run.

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

For manual/drop-in installation, each session also copies
`plugins/adt-<tier>/tools/`, `plugins/adt-<tier>/vendor/`, and
`plugins/adt-<tier>/LICENSES/` alongside the generated skills. The `vendor/` and
`LICENSES/` directories preserve the vendored `grill-me` skill and its MIT notice.

An old directory name can map to different role words for different tiers. Under later
authority, the migration creates the new role-word pointer required by each session and
does not globally repoint a shared legacy alias in a way that changes another session's
role. The migration verifies each new pointer resolves to the listed
`plugins/adt-<tier>/skills/<role-word>` source before that session adopts it.

## Compatibility and history rule

- Permanent legacy readability: classifiers and readers must continue to accept existing
  legacy role/address forms, including bare `.planner` / `.implementer` / `.reviewer` and historical
  `.orchestrator-planner` / `.orchestrator-reviewer` uses at other tiers.
- Going forward: after a seat has completed a separately authorized rename, every newly
  authored relay uses its new tier-specific role word.
- Immutable history: historical relays, indexes, ledgers, reconciliation records,
  commits, and archived session records remain unchanged. No later migration authority
  extends to those bytes, which remain readable in their original form.

## Report-back shape after a later authorized migration

After a later authorized migration, each agent's report identifies its old address, its
chosen new address, the Claude and/or Codex role-word pointer it verified, and the
unchanged-history check. Until those reports exist and are reconciled, the honest state
remains: template delivered, rename not proven applied, and apex owner proposal not
accepted.

FINAL_GIT_STATUS_SHORT: <paste the target deployment's literal final git status --short output; use none — clean tree only when empty, or unavailable — reason>
