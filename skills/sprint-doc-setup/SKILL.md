---
name: sprint-doc-setup
description: Use when setting up or maintaining the working document tree for an orchestrator-team sprint. Requires Superpowers.
---

# Sprint Doc Setup

Use this skill to create or maintain the file substrate for one orchestrator-team sprint. This skill owns **where sprint documents and relays live**, their naming conventions, and lint-clean relay formatting. It does not define relay semantics; use the role skills' adjacent `protocol.md` for authority, dispatch, merge, scope, and evidence rules.

## Mandatory prerequisites

**Superpowers is mandatory.** Use Superpowers planning/review procedures when the sprint doc setup is part of a larger plan. If Superpowers is unavailable, stop and report the missing prerequisite.

## Output tree

Create one sprint directory:

```text
docs/sprints/YYYY-MM-DD-<topic>/
  ROADMAP.md
  RECONCILE.md
  audits/
  designs/
  plans/
  reviews/
  results/
  .relays/        # gitignored; operational relay substrate
```

Use repo-root-relative paths. Keep tracked durable docs in the sprint tree; keep operational relays in `.relays/` unless the operator directs a tracked relay archive.

## Relay tree convention

```text
.relays/<RUN_ID>/INDEX.md
.relays/<RUN_ID>/boot/<boot-dispatch-id>/<PHASE>-<ROLE>-<YYYYMMDD-HHMMSS>.md
.relays/<RUN_ID>/<DISPATCH_ID>/<PHASE>-<ROLE>-<YYYYMMDD-HHMMSS>.md
```

The `boot/` subtree exists only in orchestrator-tier (and above) runs, for orchestrator-planner `init` relays. Boot relays use the existing relay schema (`PHASE: SITREP`, `AUTHORITY: report-only`) and grant no work authority; they bring operator-relayed seats online.

`INDEX.md` is append-oriented and is the one relay-adjacent file normally skipped by relay-lint. It should include at least:

```text
| time | phase | role | dispatch | parent | from | to | cc | status | file |
```

Do not assume `.relays/` is shared across worktrees or sessions. If a receiver cannot access the path, relay the file contents verbatim or attach the file.

## Standalone pair runs

For a standalone pair run, the operator boots seats directly; do not create a `boot/` subtree. Use the same relay tree, naming conventions, and `INDEX.md` discipline otherwise. The daemon is optional, but the artifacts are not: the D7 ceremony floor still files relays, maintains an `INDEX.md`, and produces the full auditable trail.

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
```

`PARENT_DISPATCH_ID` is the work-lineage edge used by lineage gates. `IN_REPLY_TO` is local/display threading only and is not a gate input. Do not invent another threading scheme.

## Naming conventions

- `RUN_ID`: short dotted or hyphenated run id, e.g. `site-qi-2026-06-19`.
- `DISPATCH_ID`: stable per commissioned work cycle, named by work, not phase — `v29-split`, not `v29-audit-split`; successor phases reuse the cycle's ID. The role skills' adjacent `protocol.md` remains the semantic authority for cycle semantics.
- Addresses: dotted lowercase owner-role form, e.g. `qi-a.planner`, `qi-a.implementer`, `site-qi.orchestrator-planner`.
- Timestamps: `YYYYMMDD-HHMMSS` in local sprint time unless the operator specifies UTC. Read the real clock at authoring time — never infer a stamp from a neighbouring relay or a tidy cadence. `relay-lint` fails an impossible or drifted stamp, and `relay-lint --index` fails an index whose `time` column decreases or disagrees with the filename it points at.

## Lifecycle mapping

```text
BOOT/init    -> .relays/<RUN_ID>/boot/<run>-boot-<pair>-<role>/... (report-only onboarding; no work authority)
AUDIT        -> audits/<dispatch-id>.md + .relays/<RUN_ID>/<DISPATCH_ID>/...
DESIGN       -> designs/<design-lock-id>.md
PLAN         -> plans/<plan-lock-id>.md
PLAN-REVIEW  -> reviews/<dispatch-id>-plan-review.md
IMPL         -> relay dispatch + PR branch, not a tracked sprint doc by default
REVIEW-FOLD  -> reviews/<dispatch-id>-fold.md
MERGE-GATE   -> results/<dispatch-id>-merge-gate.md
LIVE-VERIFY  -> results/<dispatch-id>-live-verify.md
RECONCILE    -> RECONCILE.md updates or a tracked reconcile doc
```

## Gotchas

- Flush-left relay headers are lintable; indented headers may become prose.
- Fenced examples of `DISPATCH IMPL` / `DISPATCH MERGE` are inert; operative tokens must be bare, unfenced, un-backticked, and alone on their own line in a valid relay addressed to the actor.
- `INDEX.md` is routing context, not an authority relay.
- Do not proxy-author relays for another seat. `FROM` is your own address.
- Append each new `INDEX.md` row at the END of the file, after the last existing row, in write order; never tuck a row next to your seat's earlier row or group rows by owner/role. That grouping is a read-modify-write upsert and races during concurrent work.
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
