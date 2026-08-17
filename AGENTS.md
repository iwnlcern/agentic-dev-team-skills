# AGENTS.md

Instructions for coding agents working on this repository.

## Repo map

- `skills/`, `shared/`, `tools/`, and `vendor/` are the hand-edited canonical sources.
- `plugins/` is generated output. Never hand-edit anything under `plugins/`; edit the canonical source and regenerate.
- `tools/` carries the linter, the generator, the relay engine, their fixture matrices, and host adapters (setup guide: `tools/adapters/README.md`).
- `docs/sprints/` holds working records (`active/` for running sprints, `archive/` for closed ones); a sprint's relay trees live under `.relays/` substrates that `.gitignore` excludes from tracking, and they are immutable history — never edit or delete a filed relay.

## Commands

- Regenerate plugin trees: `python3 tools/generate-plugins.py`
- Verify generated trees match canonical sources: `python3 tools/generate-plugins.py --check`
- Self-tests: `python3 tools/check-relay-lint-fixtures.py`, `python3 tools/check-timestamp-drift.py`, `python3 tools/check-generate-plugins.py`, `python3 tools/check-fixture-readme-census.py`, and `bash tools/adapters/claude-code/test-adapter.sh`
- Engine test suite, contributor entry point (assumes a POSIX shell and `python3` at 3.11 or newer on `PATH`): `PYTHONPATH=tools PYTHONWARNINGS=error::ResourceWarning RELAY_ENGINE_RESULTS_ROOT="$(python3 -c 'import tempfile; print(tempfile.mkdtemp())')" RELAY_ENGINE_MATRIX_RUN="$(python3 -c 'import uuid; print(uuid.uuid4())')" python3 -m unittest discover -s tools/relay_engine/tests -t tools`
  Inspect the transcript: it should end with `Ran 238 tests` and exactly `OK` — any `skipped` count means the environment exports did not take effect.
  The run leaves an unretained temporary directory under the system temp root; it does not produce the engine lane's persisted acceptance artifacts, whose retained-environment proof sequence lives in that lane's plan.
- Lint one relay: `python3 tools/relay-lint.py <file>`

## Rules

- Run `python3 tools/generate-plugins.py --check` after any edit to `skills/`, `shared/`, `tools/`, `vendor/`, or the generator; commit regenerated `plugins/` output together with its canonical change.
- Provenance for every generated file lives in `plugins/PROVENANCE.json`; some generated files carry a do-not-edit banner, but the absence of a banner never means a file is hand-editable — `--check` fails on any hand edit under `plugins/` either way.
- In long-form `.md` files, put each full sentence on its own line.
- Do not add nested `AGENTS.md` files; this file is the only one.
