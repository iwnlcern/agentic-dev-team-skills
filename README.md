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

This is the opposite bet. It forces agents into separated roles with real authority boundaries, makes
them cross-check each other's work, and turns every substantive handoff into a compact, inspectable
artifact (a "relay") so the orchestrator and the human stay in control. It's built from one primitive
— the **Agent Pair** (a Planner and an Implementer who review each other's work under separated
authority) — and scales up to Orchestrator Pairs coordinating several Agent Pairs at once.

## What's inside

Six skills, under `skills/`:

| Skill | What it does |
|---|---|
| `agent-pair-planner` | the Planner half of an Agent Pair — audit, design, locked plans, review synthesis |
| `agent-pair-implementer` | the Implementer half — plan review, execution, evidence, adversarial review of the Planner's work |
| `orchestrator-planner` | coordinates two or more Agent Pairs — decomposes, routes, sequences, integrates |
| `orchestrator-reviewer` | adversarially reviews the orchestrator's own decomposition, routing, and relays |
| `design-grill` | optional pre-lock design pressure-test (wraps Matt Pocock's `grill-me`) |
| `sprint-doc-setup` | scaffolds the sprint's doc + relay tree |

Each role skill carries a `protocol.md` — the shared rules (phase headers, addressing, authority,
evidence levels, lineage, merge gating) that every role applies before it acts.

Plus **`tools/`** — `relay-lint`, a small linter that structurally checks the relay artifacts (shape,
enums, addressing, lineage, merge-token grammar; it's truth-agnostic — it checks structure, not
whether claims are true), a Claude Code auto-lint adapter, and a fixture matrix that's the linter's
own correctness gate (`python3 tools/check-relay-lint-fixtures.py`, plus
`python3 tools/check-timestamp-drift.py` for the clock-dependent checks).

Current version: **v2.8.8.3** — relay-filename timestamp-drift and index-ordering checks, so a relay
stamped with a time nobody read off a clock fails the lint instead of silently reordering the trail. The
filename carries the tight wall-clock window (±2 min); the index is checked for ordering only, so an index
that has not been appended to recently still passes.

## Install

It's shaped as a Claude Code plugin. Add the repo as a marketplace and install:

```
/plugin marketplace add iwnlcern/agentic-dev-team-skills
/plugin install agentic-dev-team-skills
```

Or just drop the `skills/` folders into your skills directory (e.g. `~/.claude/skills/`) and keep
`tools/` alongside them.

## Usage

The smallest unit is one **Agent Pair**: two agent sessions, one loaded with `agent-pair-planner`,
the other with `agent-pair-implementer`. The Planner audits, designs, and writes the locked plan; the
Implementer reviews the plan, executes only after an explicit dispatch, and has its work adversarially
reviewed back. Neither side self-approves. Every substantive handoff between them is a relay file
that you (or your orchestration layer) carry across sessions.

A typical run:

1. Run `sprint-doc-setup` once to scaffold the sprint tree — `docs/sprints/YYYY-MM-DD-<topic>/` with
   a `.relays/` substrate for the operational traffic.
2. Boot each session into its role: load the role skill, apply `protocol.md`, act only within the
   seat and phase you're addressed in.
3. Work moves phase by phase — AUDIT → DESIGN (optionally `design-grill`) → PLAN → plan review →
   dispatch → implementation → adversarial review → merge gate — with each handoff a lint-clean
   relay file.
4. Lint relays before handing them off:

   ```sh
   python3 tools/relay-lint.py path/to/relay.md            # one relay, incl. authoring-drift check
   python3 tools/relay-lint.py --relay-root .relays/<run>  # a whole run, with lineage cross-checks
   python3 tools/relay-lint.py --index .relays/<run>/INDEX.md  # index: real times, non-decreasing, not ahead of the clock
   ```

   Linting one relay also checks that its filename timestamp is a real time within ±2 min of the clock,
   so a stamp nobody read off a clock fails before the handoff. Add `--no-freshness` when re-verifying an
   older relay, `--max-drift-minutes N` to loosen the tolerance, and `--index-audit` to see history
   grandfathered by a `monotonic-from` marker. `--index` deliberately applies no drift window: it is an
   ordering check, so a quiet index passes while a row stamped ahead of the clock fails.

Running more than one pair at once is what the orchestrator roles are for: `orchestrator-planner`
decomposes and routes work across pairs, and `orchestrator-reviewer` adversarially checks the
orchestrator itself — the decomposition, the routing, and the relays.

## Built with it

[frank](https://github.com/iwnlcern/frank) — a governed courier for multi-agent teams, written in Go
— was built end-to-end by agent teams running these skills: six slices, each closed by an adversarial
merge gate, run by paired planner/implementer teams under a standing orchestrator pair. That repo
ships its entire working record — the full relay trail (540 relays under `.relays/`), the per-slice
ledgers and gate evidence (`docs/sprints/`), and the governing team's workspace (`master-docs/`) — so
if you want to see what these roles, phases, and relays look like under real load, that's the place
to read.

It's also the natural next layer: these skills are conventions, and a sufficiently confused agent can
break a convention. frank is the enforcement counterpart — it takes the parts these skills can only
ask for politely (identity, lineage, gate outcomes) and makes them structural.

## Requirements

- **Superpowers** — the skills build on its brainstorming, planning, plan-execution, and review
  procedures. If it's not installed, the skills tell the agent to stop and report the missing
  prerequisite.
- **Python 3** — only if you want `relay-lint` (in `tools/`). The skills reference it but degrade
  gracefully: they note when it's unavailable rather than failing. Install `tools/` under your skills
  root, or point at it however your host resolves skills.

## License

Apache-2.0 — see `LICENSE`. The vendored `grill-me` skill under `vendor/mattpocock/grill-me/` is MIT
© 2026 Matt Pocock; its license is in `LICENSES/`.

## Status

Personal project. I use it, I tweak it, no promises. The rule I hold myself to: if something in here
doesn't change an agent's behavior under pressure, it isn't load-bearing and it gets cut.
