# agentic dev team skills

A kit of skills for running software work as a **team of agents** instead of one agent one-shotting a
diff. It's a coordination protocol, not a runtime — it defines roles, phase boundaries, who's allowed
to do what, how agents hand work off, and what counts as evidence. It rides on top of whatever host
and method skills actually do the work.

## How it works

Most agent tooling is optimized for fast one-shotting: one agent, one context, ship the diff.
Multi-agent setups mostly just parallelize that — more agents one-shotting at once — and inherit the
same failure mode: context fragments across agents, decisions get made on partial views, and
plausible-but-wrong work survives because every checker shares the author's blind spots and the
deadline.

This is the opposite bet. It separates planning, implementation, and review authority; makes roles
cross-check each other's work; and turns every substantive handoff into a compact, inspectable
artifact (a "relay") so the governing team and the human stay in control. A two-seat pair is the
smallest unit. Higher tiers decompose, route, commission, and arbitrate work across nested teams.

## What's inside

### Four-tier vocabulary

The tier lives in the role word; there is no separate tier field.

| Tier | Seats | Who implements in the codebase |
|---|---|---|
| Pair | Pair Planner and Pair Implementer | Pair Implementer |
| Orchestrator | Orchestrator Planner and Orchestrator Reviewer | Downstream Pair Implementers |
| Domain | Domain Planner and Domain Reviewer | Commissioned sub-team Pair Implementers |
| Master | Master Planner and Master Reviewer | Commissioned sub-team Pair Implementers |

Legacy bare forms remain valid and class at pair tier: `Planner`, `Implementer`, and `Reviewer` are
pair-tier equivalents. Review panels are ephemeral lenses, not addressable roles. Naming buys
clarity, not enforcement — every seat retains disk-write authority.

#### Master tier — what v2.9 does and does not claim

v2.9 claims the proven mechanics: decomposition/routing, commissioning, an arbitration round trip,
ledgers, and charters. It does not claim these provisional boundaries are mechanically enforced:

1. nested-run lineage is declared, not verified — interim: sub-teams answer up via relays with the authorizing dispatch named in the local charter, escalations thread through named dispatch dirs, an unauthorized child trail is caught by reconciliation, not by a gate;
2. rulings do not bind downward mechanically — interim: hand-relayed amendment cascade and operator retire-and-reboot of stale sessions;
3. no containment — tier words state who implements; nothing confines a seat's writes.

All three close when v29-engine lands; procedure text then updates in a v2.9.x edit with no label to lift.

### Three nested plugins, ten skills

Each higher plugin contains the complete lower-tier bundle, so installing one plugin is enough for
that tier and everything below it.

| Plugin | Skills in its generated bundle |
|---|---|
| `adt-pair` | `pair-planner`, `pair-implementer`, `design-grill`, `sprint-doc-setup` |
| `adt-orchestrator` | Everything in `adt-pair`, plus `orchestrator-planner`, `orchestrator-reviewer` |
| `adt-master` | Everything in `adt-orchestrator`, plus `domain-planner`, `domain-reviewer`, `master-planner`, `master-reviewer` |

Every generated role skill carries its adjacent `protocol.md` and tier-specific shared assets. The
repository's canonical `skills/` and `shared/` directories are the semantic sources for the
generator.

Plus **`tools/`** — `relay-lint`, a small linter that structurally checks relay artifacts (shape,
enums, addressing, lineage, merge-token grammar; it is truth-agnostic — it checks structure, not
whether claims are true), a Claude Code auto-lint adapter, and a fixture matrix that is the linter's
own correctness gate (`python3 tools/check-relay-lint-fixtures.py`, plus
`python3 tools/check-timestamp-drift.py` for clock-dependent checks).

Current version: v2.9.3 — four-tier vocabulary, three nested plugins, canonical-source generator.

## Install

The generated trees under `plugins/` are the only install artifact. Add the repository as a Claude
Code marketplace, then install the tier you need:

```text
/plugin marketplace add iwnlcern/agentic-dev-team-skills
/plugin install adt-pair
/plugin install adt-orchestrator
/plugin install adt-master
```

Manual/drop-in: copy `plugins/adt-<tier>/skills/*` into the host skills root and
`plugins/adt-<tier>/tools/`, `plugins/adt-<tier>/vendor/`, and
`plugins/adt-<tier>/LICENSES/` alongside. The generated tree is itself the drop-in bundle.
For Claude Code the user skills root is `~/.claude/skills/`; the auto-lint hook setup is
documented in `tools/adapters/README.md`.

- Codex: install via the plugin route, same repository, no separate artifact (verified 2026-08-20
  on codex-cli 0.149.0 for adt-pair, adt-orchestrator, and adt-master):

  ```text
  codex plugin marketplace add iwnlcern/agentic-dev-team-skills
  codex plugin add adt-<tier>@agentic-dev-team-skills
  ```

  Codex discovers `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` natively, so
  the Claude Code marketplace layout serves both hosts unchanged, with skill adjacency intact.
  Fallback (air-gapped installs, or any other `.agents/skills` host): copy
  `plugins/adt-<tier>/skills/*` into `.agents/skills/` (project) or `~/.agents/skills/` (user), and
  `plugins/adt-<tier>/tools/` to `~/.agents/skills/tools/`; `~/.codex/skills` remains a working
  deprecated destination. Manual copy needs no manifest (filesystem discovery:
  https://learn.chatgpt.com/docs/build-skills).

Do not install the canonical `skills/` dirs directly. They are sources, not install artifacts;
installing them directly ships broken skills (no adjacent `protocol.md`).

### Migrating existing installs

The rename `agentic-dev-team-skills` → `adt-orchestrator` is content-preserving. Automatic migration
requires Claude Code ≥ v2.1.193. Upgrade Claude Code first, then run:

```text
/plugin marketplace update
```

Clients below that floor must uninstall `agentic-dev-team-skills` and install `adt-orchestrator`
manually. For managed or enterprise installations, an administrator must update `enabledPlugins`
from the old plugin name to `adt-orchestrator`.

Manual installs migrate by renaming the skill dirs (skill-dir names now equal role words):

- historical `agent-pair-planner` → `pair-planner`
- historical `agent-pair-implementer` → `pair-implementer`

## Usage

Operator quickstart: open one session per seat and load its role skill. For a standalone pair, boot the
Planner and Implementer sessions directly. For a multi-pair run, boot the Orchestrator Planner and
trigger `init`; then carry each returned file-first hand-off pointer to its exact `TO` seat and treat
`CC` as context only.

The smallest unit is one pair-tier team: two agent sessions, one loaded with `pair-planner` and the
other with `pair-implementer`. The Pair Planner audits, designs, writes the locked plan, and reviews
implementation evidence. The Pair Implementer independently audits, reviews the plan, executes only
after an explicit dispatch, and performs adversarial review. Neither side self-approves. Every
substantive handoff between them is a relay file that you or your orchestration layer carries across
sessions.

A typical run:

1. Run `sprint-doc-setup` once to scaffold the sprint tree — `docs/sprints/active/YYYY-MM-DD-<topic>/` with
   a `.relays/` substrate for operational traffic.
2. Boot each session into its role: load the role skill, apply `protocol.md`, and act only within the
   seat and phase addressed to it.
3. Work moves phase by phase — AUDIT → DESIGN (optionally `design-grill`) → DESIGN-REVIEW → PLAN → plan review →
   dispatch → implementation → adversarial review → merge gate — with each handoff a lint-clean
   relay file.
4. Lint relays before handing them off:

   ```sh
   python3 tools/relay-lint.py path/to/relay.md            # one relay, incl. authoring-drift check
   python3 tools/relay-lint.py --relay-root .relays/<run>  # a whole run, with lineage cross-checks
   python3 tools/relay-lint.py --index .relays/<run>/INDEX.md  # index: real times, non-decreasing, not ahead of the clock
   ```

   Linting one relay also checks that its filename timestamp is a real time within ±2 min of the
   clock, so a stamp nobody read off a clock fails before the handoff. Add `--no-freshness` when
   re-verifying an older relay, `--max-drift-minutes N` to loosen the tolerance, and `--index-audit`
   to see history grandfathered by a `monotonic-from` marker. `--index` deliberately applies no
   drift window: it is an ordering check, so a quiet index passes while a row stamped ahead of the
   clock fails.

Running more than one pair at once is what the orchestrator roles are for: `orchestrator-planner`
decomposes and routes work across pair-tier teams, and `orchestrator-reviewer` adversarially checks
the orchestrator itself — its decomposition, routing, and relays. The domain and master tiers add
spec-of-record ownership, commissioned sub-teams, and cross-domain arbitration without moving
implementation into governance seats.

## Requirements

- **Superpowers** — the skills build on its brainstorming, planning, plan-execution, and review
  procedures. If it is not installed, the skills tell the agent to stop and report the missing
  prerequisite.
- **Python 3** — only if you want `relay-lint` (in `tools/`). The skills reference it but degrade
  gracefully: they note when it is unavailable rather than failing. Install the generated `tools/`
  directory under your skills root, or point at it however your host resolves skills.

## License

Apache-2.0 — see `LICENSE`. The vendored `grill-me` skill under `vendor/mattpocock/grill-me/` is MIT
© 2026 Matt Pocock; its license is in `LICENSES/`.

## Status

Personal project. I use it, I tweak it, no promises. The rule I hold myself to: if something in here
doesn't change an agent's behavior under pressure, it isn't load-bearing and it gets cut.
