# Testing practice

This file is the one home of the kit's test practice (DD-v3-tests § 3.1); other documents point here instead of restating it.

## Everyday

Run the test modules for what you changed, and check the trap list below for tests that read your paths from elsewhere.
When you are unsure which modules apply, run the whole engine suite.
A plan's verification lines name test modules, never the whole suite.
No test session runs over 30 minutes (R265).
Every check names the decision its result can change, or it does not run (R250.5).
No test asserts a wall-clock duration as pass or fail.
A test that starts a long-lived child (a daemon, a server, a watcher) passes it the inherited environment, because the release check finds a step's leftover descendants by an environment marker (`ADT_RELEASE_STEP`); a child started with an environment built from scratch escapes that sweep.

Run one engine module from the repository root:

    PYTHONPATH=tools python3 -m unittest relay_engine.tests.<module>

Run the release runner's own tests (they need Docker):

    python3 -m unittest discover -s tools/release_check_tests -v

## The release check

The full run happens once, at the release head, through the release check:

    python3 tools/release-check.py full --out <a directory under $HOME>

It runs every step on two legs side by side: `mac` on this host and `linux` in a Docker container built from `tools/release-check.Dockerfile` (Python 3.11, linux/aarch64 on this machine).
Each leg is cut at 30 minutes, and anything unfinished at the cap counts as failed.
A failure on either leg fails the check, and the runner never turns a failure green.
The results go to `<out>/release-check.json`, with one `results.json` per leg; `--out` must be under `$HOME`, since macOS purges `/tmp` and `/var/folders`.
At the release head, add `--release`, which refuses to start while any exclusion in the runner's table remains.

## Trap list

Each line names changed paths whose dependent tests live elsewhere, so a run of only the obvious modules would miss them.
Each entry was re-measured on the tree by searching the test modules and checks for the paths they read, including through imported helpers.

- `skills/**`, `shared/**`, `vendor/**`, `LICENSE*`, `tools/generate-plugins.py` → `test_e2e_chain`, `test_cold_install`, `test_version`, `check-generate-plugins.py`, `generate-plugins.py`
- `plugins/**` → `test_version`, `test_e2e_chain`, `check-generate-plugins.py`, `generate-plugins.py`
- `tools/relay_engine/*.py` → `test_version`, `test_e2e_chain`, `test_cold_install`, `check-generate-plugins.py`
- `tools/relay_engine/errors.py`, `tools/relay_engine/strings.py` → `test_errors`, `test_strings_gate`
- `tools/relay-lint-fixtures/**` → `test_lint_relay_root_record`, `test_judging`, `test_judging_agreement`, `test_draft_shape_lint`, `test_finding_tags`, `test_e2e_chain`, `test_gate_review`, `test_human_gate`, `test_parity`, `test_merge_claim`, `test_fixture_seam_copy`, `test_mtbattery_guard`, `capture_order_baseline`, `test-adapter.sh`, `check-relay-lint-fixtures.py`, `check-fixture-readme-census.py`, `check-timestamp-drift.py`, `check-generate-plugins.py`

Notes on the entries:

- A new or removed engine module also changes the engine roster (`version.py` `ROSTER`) and `LOCKED_ENGINE_SET` in `check-generate-plugins.py`, which those tests compare.
- A new error code also joins `_CODE_VALUES` (`tools/relay_engine/strings.py`), the all-members list the string gate checks.
- `test_cold_install` reads `skills/` through `ruled_manifest`, which it imports from `test_e2e_chain`, and `test_version` reads `skills/`, `shared/`, `vendor/` and `LICENSE*` through the generator's `generate_tree`.
- The `tools/relay-lint-fixtures/**` entry lasts until those fixtures are deleted (DD-v3-tests stage 4).
- The kit pair adds the entry for `tools/kit_tests/` when that directory lands.
