# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the project's own release numbering.

## [2.9.3] — 2026-08-25

A docs-only hotfix, smaller still than 2.9.2: one corrected status paragraph and one init directive.

- **The engine's own README no longer claims "the relay engine is not adopted".**
  The status paragraph predated the 2.9.1 production adoption — the very claim 2.9.2 removed from `sprint-doc-setup` survived here under a different spelling ("not adopted" vs "not yet adopted") and escaped that release's phrase-exact survey; a consumer master session read it, took hand mode as current practice, and never surfaced the mode choice to its operator.
  The README now states the same operator-chosen mode split as `sprint-doc-setup`, with `relay migrate` as the only path between modes.
  The README is engine-roster content, so the fingerprint moves; the kit version bumps because plugin update machinery propagates on the version string.
- **`sprint-doc-setup` init now requires the operator's mode choice before the first boot relay.**
  The booting seat asks whether the run's root is engine-managed or hand-authored and records the answer in its first relay — the mode is never inferred from a document or a default.

## [2.9.2] — 2026-08-24

A migration hotfix: two small fixes found by the first consumer migrations, nothing else.
The number is one patch step, but the change is one migrate carve-out and one corrected paragraph — far smaller than the jump from 2.9 to 2.9.1.

- **`migrate check`/`cutover` gain the legacy carve-out.**
  Unparseable pre-engine relay bytes are inventoried as immutable history instead of redding the verdict and refusing cutover; the receipt records the malformed set, and cutover excludes exactly that set (a drifted set still refuses).
  Found by the pdc-bivpak migration, whose pre-2.9 roots could never cut over as shipped (the harness-frank migration hit the same wall and worked around it with a fresh root).
- **`sprint-doc-setup` no longer claims the daemon is "not yet adopted".**
  The stale operator-quickstart clause predated the engine's production adoption and instructed seats to hand-maintain `INDEX.md` unconditionally — in an engine-managed root that is a defect, not the ceremony floor.
  The paragraph now states the mode split: engine-managed roots file through `relay submit` with the daemon as the only `INDEX.md` writer; hand-authored roots (pre-cutover or rolled back) keep the D7 gated-append floor.
  Found by the harness-frank migration; shipped into all three plugin tiers by regeneration.
- Kit identity 2.9.1 → 2.9.2; the engine suite measures 361 tests (`AGENTS.md:18`).

## [2.9.1] — 2026-08-23

A distribution-hardening patch: the relay engine ships inside every plugin bundle, Codex becomes a verified plugin host with write-time relay-lint hooks, and the orchestrator/master pair skills become derived subordinate variants.

### Added

- **Engine distribution.**
  The relay engine and CLI ship inside all three plugin bundles under `tools/`, with adapter-ladder resolution switched to the bundled engine and version/fingerprint identity reporting (`relay version`) for consumer repointing.
- **Dual-host relay-lint hooks.**
  `hooks/hooks.json` ships in every bundle: the PostToolUse normalizer gains additive apply_patch envelope-target coverage — exact-target lint with silent-clean success, an explicit noisy UNLINTED fallback, and per-pass dedup of repeated identical targets — while the Bash guard is unchanged by the hooks work itself — its only change this release is the ladder switch above; a live-Codex witness proves the one-time trust prompt, blocked-invalid feedback, and silent-clean behavior on codex-cli 0.149.0.
- **Codex plugin install route.**
  The README's Codex section now leads with the verified `codex plugin marketplace add` / `codex plugin add` route; manual `.agents/skills` copy is the documented fallback for air-gapped installs.
- **Derived pair-skill variants (option C).**
  The adt-orchestrator and adt-master pair skills are derived artifacts — a tier-neutral subordination preamble plus the adt-pair base body — materialized from `skills/variants/` by the generator, drift-guarded in both check carriers, each shipped copy with single-source provenance.
- **Relay-tree lint.**
  The engine gains typed relay-tree integrity lint with hardened cursor/index gating, growing the engine suite to 360 tests.

### Changed

- **`HUMAN_GATE_REQUIRED` annotation warning.**
  A bare `yes` without the required annotation now warns, in the standalone linter and the engine's port alike.
- **Engine hygiene and parity hardening.**
  The v2.9.1 hygiene items land across the engine (including the relay-root xroot mechanism and the cf1 lint port), with archive-extraction parity witnessed at every stack position.

## [2.9.0] — 2026-08-16

The largest release to date: a third seat tier, a hardened relay linter with a mutation-tested fixture corpus, a native relay engine at verified parity, and tier-scoped plugin distribution.

### Breaking / migration

- **The root plugin is removed and renamed.**
  `.claude-plugin/plugin.json` at the repository root is deleted; the marketplace now serves three tier plugins, and the old `agentic-dev-team-skills` install name maps to `adt-orchestrator` via the marketplace `renames` table.
  Automatic migration requires **Claude Code ≥ v2.1.193**; older clients must uninstall `agentic-dev-team-skills` and install `adt-orchestrator` (or another tier) by hand; managed installs need an administrator to update `enabledPlugins`; manual installs must rename the skill directories `agent-pair-planner` → `pair-planner` and `agent-pair-implementer` → `pair-implementer`.
- **The canonical `skills/` directories are no longer drop-in installable.**
  Their per-skill `protocol.md` and reviewer material moved to `shared/`, so installing `skills/` directly ships broken skills.
  They are sources; the generated `plugins/adt-<tier>/` bundles are the install artifacts.

### Added

- **Master tier.**
  A four-tier role vocabulary — Master Planner/Reviewer, Domain Planner/Reviewer, Pair Planner/Implementer, and an `Operator` role class — with seat-authority text for every tier, commissioning chains, charter identity, and master-tier boundary rules.
  Ten skills ship: `design-grill`, `domain-planner`, `domain-reviewer`, `master-planner`, `master-reviewer`, `orchestrator-planner`, `orchestrator-reviewer`, `pair-implementer`, `pair-planner`, `sprint-doc-setup`.
- **Relay engine (preview — not yet adopted).**
  A daemon-backed relay creation and admission system under `tools/relay_engine/`: append-only frame-verified ledger, seat registration, envelope grammar, CLI (`tools/relay`), and a 214-test suite with a frozen parity oracle.
  Its native rules are at verified parity with the standalone linter over a 1,644-file corpus, proven by golden comparison plus perturbation and portability falsifiers.
  The shipped skills workflow remains current practice; the engine ships inert behind its own entry points.
- **Mutation battery and oracle instruments.**
  `tools/mtbattery.py` and `tools/mtbattery-spec.py` — a data-only mutation battery over the linter (90 arms, per-arm declared kill sets, zero-survivor gate) — plus `tools/mtfixgen.py` and `tools/a5fixgen.py` (fixture generation) and independent oracles (`check-index-oracle.py`, `check-t11-closure.py`, `check-dispatch-head.py`, `check-s375-escaped-rows.py`).
- **Fixture corpus.**
  1,394 added fixture paths (1,377 content files) across eleven new families — `mastertier/` (789), `t11closure/` (309), `lockdigest/` (125), `rolevocab/` (94), `rootres/` (23), `ambiguity/` (18), `hardening/` (13), `kr8a/` (9), `lockpath/` (6), `rootindex/` (5), `indexmarker/` (3) — under a 632-registration self-check harness that runs in normal and neutral-cwd modes.
- **Tier-scoped plugins.**
  Three generated plugin bundles — `adt-pair` ⊂ `adt-orchestrator` ⊂ `adt-master`, each at version 2.9.0 — produced from canonical sources by `tools/generate-plugins.py` with a verifying `--check` mode, per-file `PROVENANCE.json`, and `check-generate-plugins.py` as its own self-test.
- **Host adapters.**
  A new advisory Bash-channel relay guard (`tools/adapters/claude-code/bash-relay-guard.sh`) covering redirection/`tee`/`cp`/`mv` writes into relay trees; the settings snippet gains a `Bash` matcher and a `PostToolUseFailure` block; the post-tool hook now lints `INDEX.md` through `--index` mode instead of skipping it; both hooks also match visible `relays/` directories.
  Two operator-facing environment variables: `RELAY_LINT_MAX_DRIFT_MINUTES` and `RELAY_LINT_NO_FRESHNESS=1`.
- **Codex host support (text-level).**
  `shared/harness-codex.md` ships in every role skill of every bundle, and the linter resolution chain adds `$HOME/.agents/skills` while demoting `~/.codex/skills` to a deprecated fallback.
- **Sprint-doc conventions.**
  `sprint-doc-setup` now scaffolds under `docs/sprints/active/` with `docs/sprints/archive/` first-class, uses `.relays/<RUN_ID>/<cycle-id>/` substrate segments, leaves substrate tracking to operator discretion declared in the sprint ROADMAP, and requires a `root: <path>` marker in every relay INDEX — lint-enforced, backed by the new `rootindex/` and `rootres/` fixture families.
- **`AGENTS.md`** — a contributor-facing contract at the repository root: working-record conventions, the relay-substrate ignore rule, and the engine test suite's documented entry point.
- **`shared/design-request-template.md`** — a new DESIGN-request template (pair Planner → pair Implementer), shipped to `pair-planner` in every bundle.
- **`NOTICE`** carrying the project copyright under the Apache-2.0 convention.

### Changed

- **`tools/relay-lint.py` grew its largest hardening pass** (+1,939/−41 lines):
  every-occurrence conflict detection for authority-critical header fields (fail-closed on multiple distinct values), the master-tier gate series (H26, H27, H28, H29, H32), structural delegation (`DELEGATED_DISPATCH_AUTHORITY` is the only delegation carrier), lock-digest shape validation, commissioning-chain resolution (authorization → charter → approval → grant), the INDEX `root:` marker rule, and checkout-independent diagnostics (index verdicts name `path.name`, never absolute paths).
- **Shared protocol text** extracted from per-skill copies into `shared/` and fanned out per tier by the generator; the pair skills renamed from `agent-pair-planner`/`agent-pair-implementer` to `pair-planner`/`pair-implementer` (legacy bare role forms remain valid in relays, a stated design commitment in the vocabulary itself).
- **Orchestrator procedure texts revised**, including a rewritten `handoff-templates.md` and updates to `sitrep-reconciliation.md`, `orchestration-moves.md`, and `design-grill`.
- **License clarified to Apache-2.0.**
  The `LICENSE` body is the Apache License 2.0 text, matching the README and every generated plugin manifest; the vendored `grill-me` skill under `vendor/` remains MIT under its own license.
- `.gitignore` in this repository excludes `docs/sprints/**/.relays/` so contributors cannot accidentally commit relay substrates (adopters' own tracking posture is operator discretion, per `sprint-doc-setup`).

### Removed

- `.claude-plugin/plugin.json` (the root plugin manifest) — see Breaking / migration above.
  This is the release's only file deletion; everything else classified as a rename/relocation.

### Deprecated

- The standalone `tools/relay-lint.py` is scheduled for replacement by the relay engine's native rules once the engine is adopted; the parity port that gates that switch shipped in this release.
  Until the switch, the standalone remains the authoritative linter and both implementations are held at parity.
- `~/.codex/skills` as a linter resolution root, in favor of `$HOME/.agents/skills`.

### Known limitations

- **The master tier's boundaries are conventions, not containment.**
  Nested-run lineage is declared, not verified; rulings do not bind downward mechanically; tier words state who implements, and nothing confines a seat's writes.
- The Bash relay guard is advisory coverage, not containment: literal paths only, with variable/`cd`-relative paths, interpreter one-liners, `sed -i`, `git apply`/`checkout`, symlinked roots, and backgrounded final state unverified; any `.md` under a directory named `relays/` may draw a non-blocking advisory even when it is not a relay.
- No Codex relay-write adapter ships; the Claude Code hook is the only automated gate, and Codex users run the linter manually before every handoff.
- One historical relay in the release's own working records trips the hardened every-occurrence gate (registered as KR-9, tree-scoped; filed records are immutable by design).
- The engine's frozen-oracle fixture population orders unstamped same-dispatch relays by file mtime; trees materialized by mtime-clobbering copies can flip one leg (fresh `git clone`/checkout reproduces the frozen result; a deterministic tiebreak is planned for 2.9.1).
- The engine suite's zero-skip result requires its two documented environment variables; a bare run self-skips one census test by design.

## [2.8.8.3] — 2026-08

### Changed

- Split the relay checks: filename timestamps get the clock window; indexes get ordering.
- **Breaking:** the default freshness window tightened from ±15 to ±2 minutes (`DEFAULT_MAX_DRIFT_MINUTES`), and `--max-drift-minutes` inverted its documented role from tightening to loosening.

### Removed

- `lint_relay_index()` lost its `freshness=` and `max_drift_minutes=` keyword parameters; `--no-freshness` and `--max-drift-minutes` no longer affect `--index` mode at all.

## [2.8.8.2] — 2026-08

### Added

- Relay timestamp-drift and index-monotonicity checks.

## [1.0.0] — 2026-08

### Added

- Initial public shape: role skills and the relay linter.
