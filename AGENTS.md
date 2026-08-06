# AGENTS.md

Instructions for coding agents working on this repository.

## Repo map

- `skills/` and `shared/` are the hand-edited canonical sources.
- `plugins/` is generated output. Never hand-edit anything under `plugins/`; edit the canonical source and regenerate.
- `tools/` carries the linter, the generator, their fixture matrices, and host adapters.
- `docs/sprints/` holds working records; relays under `.relays/` are immutable history — never edit or delete a filed relay.

## Commands

- Regenerate plugin trees: `python3 tools/generate-plugins.py`
- Verify generated trees match canonical sources: `python3 tools/generate-plugins.py --check`
- Linter self-test: `python3 tools/check-relay-lint-fixtures.py` and `python3 tools/check-timestamp-drift.py`
- Lint one relay: `python3 tools/relay-lint.py <file>`

## Rules

- Run `python3 tools/generate-plugins.py --check` after any edit to `skills/`, `shared/`, or the generator; commit regenerated `plugins/` output together with its canonical change.
- Files under `plugins/` carry a do-not-edit banner naming their canonical source; a hand edit there is a failing check, not a shortcut.
- In long-form `.md` files, put each full sentence on its own line.
- Do not add nested `AGENTS.md` files; this file is the only one.
