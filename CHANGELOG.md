# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the project's own release numbering.

## [2.9.0] — 2026-08-16

The largest release to date: a third seat tier, a hardened relay linter with a mutation-tested fixture corpus, a native relay engine at verified parity, and tier-scoped plugin distribution.

### Added

- **Master tier.**
  A four-tier role vocabulary — Master Planner/Reviewer, Domain Planner/Reviewer, Pair Planner/Implementer, and an `Operator` role class — with seat-authority text for every tier, commissioning chains, charter identity, and master-tier boundary rules.
  Ten skills ship: `design-grill`, `domain-planner`, `domain-reviewer`, `master-planner`, `master-reviewer`, `orchestrator-planner`, `orchestrator-reviewer`, `pair-implementer`, `pair-planner`, `sprint-doc-setup`.
- **Relay engine (preview — not yet adopted).**
  A daemon-backed relay creation and admission system under `tools/relay_engine/`: append-only frame-verified ledger, seat registration, envelope grammar, CLI (`tools/relay`), and a 214-test suite with a frozen parity oracle.
  Its native rules are at verified parity with the standalone linter over a 1,644-file corpus, proven by golden comparison plus perturbation and portability falsifiers.
  The shipped skills workflow remains current practice; the engine ships inert behind its own entry points.
- **Mutation battery and oracle instruments.**
  `tools/mtbattery.py` and `tools/mtbattery-spec.py` — a data-only mutation battery over the linter (90 arms, per-arm declared kill sets, zero-survivor gate) — plus `tools/mtfixgen.py` (fixture generation) and independent oracles (`check-index-oracle.py`, `check-t11-closure.py`, `check-dispatch-head.py`, `check-s375-escaped-rows.py`, `a5fixgen.py`).
- **Fixture corpus.**
  1,394 new linter fixtures across eleven families — `mastertier/` (789), `t11closure/` (309), `lockdigest/` (125), `rolevocab/` (93), and others — under a 632-registration self-check harness that runs in normal and neutral-cwd modes.
- **Tier-scoped plugins.**
  Three generated plugin bundles — `adt-pair` ⊂ `adt-orchestrator` ⊂ `adt-master`, each at version 2.9.0 — produced from canonical sources by `tools/generate-plugins.py` with a verifying `--check` mode, per-file `PROVENANCE.json`, and `check-generate-plugins.py` as its own self-test.
- **`AGENTS.md`.**
  A contributor-facing contract at the repository root: working-record conventions, the relay-substrate ignore rule, and the engine test suite's documented entry point.
- **`NOTICE`** carrying the project copyright under the Apache-2.0 convention.

### Changed

- **`tools/relay-lint.py` grew its largest hardening pass** (+~1,900 lines):
  every-occurrence conflict detection for authority-critical header fields (fail-closed on multiple distinct values), the H26–H32 master-tier gate series, structural delegation (`DELEGATED_DISPATCH_AUTHORITY` is the only delegation carrier), lock-digest shape validation, exact commissioning-chain resolution, and checkout-independent diagnostics (index verdicts name `path.name`, never absolute paths).
- **Shared protocol text** extracted from per-skill copies into `shared/` and fanned out per tier by the generator; the pair skills renamed from `agent-pair-planner`/`agent-pair-implementer` to `pair-planner`/`pair-implementer` (legacy bare role forms remain permanently valid in relays).
- **License clarified to Apache-2.0.**
  The `LICENSE` body is the canonical Apache License 2.0 text, matching the README and every generated plugin manifest; the vendored `grill-me` skill under `vendor/` remains MIT under its own license.
- `.gitignore` excludes `docs/sprints/**/.relays/` so adopters cannot accidentally commit relay substrates.

### Deprecated

- The standalone `tools/relay-lint.py` is scheduled for replacement by the relay engine's native rules once the engine is adopted; the parity port that gates that switch shipped in this release.
  Until the switch, the standalone remains the authoritative linter and both implementations are held at parity.

### Known limitations

- One historical relay in the release's own working records trips the hardened every-occurrence gate (registered as KR-9, tree-scoped; filed records are immutable by design).
- The engine's frozen-oracle fixture population orders unstamped same-dispatch relays by file mtime; trees materialized by mtime-clobbering copies can flip one leg (fresh `git clone`/checkout reproduces the frozen result; a deterministic tiebreak is planned for 2.9.1).
- The engine suite's zero-skip result requires its two documented environment variables; a bare run self-skips one census test by design.

## [2.8.8.3] — 2026-08

### Changed

- Split the relay checks: filename timestamps get the clock window; indexes get ordering.

## [2.8.8.2] — 2026-08

### Added

- Relay timestamp-drift and index-monotonicity checks.

## [2.8.8] — 2026-08

### Added

- Initial public shape: role skills and the relay linter.
