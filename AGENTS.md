# AGENTS.md

Instructions for coding agents working on this repository.

## Repo map

- `skills/`, `shared/`, `tools/`, and `vendor/` are the hand-edited canonical sources.
- `plugins/` is generated output. Never hand-edit anything under `plugins/`; edit the canonical source and regenerate.
- `tools/` carries the linter, the generator, their fixture matrices, and host adapters.
- `docs/sprints/` holds working records; a sprint's relay trees (kept under a gitignored `.relays/` substrate) are immutable history — never edit or delete a filed relay.

## Commands

- Regenerate plugin trees: `python3 tools/generate-plugins.py`
- Verify generated trees match canonical sources: `python3 tools/generate-plugins.py --check`
- Self-tests: `python3 tools/check-relay-lint-fixtures.py`, `python3 tools/check-timestamp-drift.py`, `python3 tools/check-generate-plugins.py`, and `bash tools/adapters/claude-code/test-adapter.sh`
- Lint one relay: `python3 tools/relay-lint.py <file>`

## Rules

- Run `python3 tools/generate-plugins.py --check` after any edit to `skills/`, `shared/`, `tools/`, `vendor/`, or the generator; commit regenerated `plugins/` output together with its canonical change.
- Provenance for every generated file lives in `plugins/PROVENANCE.json`; some generated files carry a do-not-edit banner, but the absence of a banner never means a file is hand-editable — `--check` fails on any hand edit under `plugins/` either way.
- In long-form `.md` files, put each full sentence on its own line.
- Do not add nested `AGENTS.md` files; this file is the only one.
