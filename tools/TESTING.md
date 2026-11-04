# Testing practice

This file is the one home of the kit's test practice (DD-v3-tests § 3.1); other documents point here instead of restating it.

## Everyday

Run the test modules for what you changed, and check the trap list below for tests that read your paths from elsewhere.
When you are unsure which modules apply, run the whole engine suite.
A plan's verification lines name test modules, never the whole suite.
No test session runs over 30 minutes (R265).
Every check names the decision its result can change, or it does not run (R250.5).
No test asserts a wall-clock duration as pass or fail.
A test that starts a long-lived child (a daemon, a server, a watcher) passes it the inherited environment, because the release check finds a step's leftover descendants by an environment marker (`ADT_RELEASE_STEP`) or by the step's process group; a child started in a new session with an environment built from scratch escapes that sweep.
On macOS the sweep cannot see the environment of platform binaries (`/bin`, `/usr/bin` and the other system directories), so a detached shell or system tool cannot be attributed to its step.
The mac leg lists such processes that started during the leg with a working directory in the repository or the leg's output directory as `unattributed` in `results.json`, and never kills them; the linux leg stops them like any other leftover.
A test that detaches such a child stops it itself.

Run one engine module from the repository root, with the results environment the engine tests require (`test_e2e_chain` skips its census test without it):

    PYTHONPATH=tools PYTHONWARNINGS=error::ResourceWarning RELAY_ENGINE_RESULTS_ROOT="$(mktemp -d "$HOME/.cache/relay-engine-results.XXXXXX")" RELAY_ENGINE_MATRIX_RUN="$(python3 -c 'import uuid; print(uuid.uuid4())')" python3 -m unittest relay_engine.tests.<module>

The summary must read `OK` with no `skipped=` count; the release check fails a module that skips a test or runs none.

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
Every row uses one rule: a test module or check is listed when its own code reads the row's path, or when it imports a helper or loads a script whose code it uses does; modules the release check excludes are left out.
The rows were measured on the tree against the v3 test audit's reverse map.

- `skills/**`, `shared/**`, `vendor/**`, `LICENSE*`, `tools/generate-plugins.py` → `test_e2e_chain`, `test_cold_install`, `test_version`, `check-generate-plugins.py`, `generate-plugins.py`
- `plugins/**` → `test_version`, `test_e2e_chain`, `check-generate-plugins.py`, `generate-plugins.py`
- `tools/relay_engine/*.py` → `test_version`, `test_e2e_chain`, `test_cold_install`, `test_relay_monitor`, `test-adapter.sh`, `check-generate-plugins.py`, `generate-plugins.py`
- `tools/relay` → `check-generate-plugins.py`, `test-adapter.sh`, `test_relay_monitor`
- `tools/**` → `check-generate-plugins.py`, `test_identity_matrix`, `test_mtbattery_guard`, `test_version`
- `tools/adapters/**` → `test-adapter.sh`, `check-generate-plugins.py`, `generate-plugins.py`
- `tools/relay_engine/errors.py`, `tools/relay_engine/strings.py` → `test_errors`, `test_strings_gate`
- `tools/relay-lint.py` → `check-fixture-readme-census.py`, `check-generate-plugins.py`, `check-relay-lint-fixtures.py`, `check-timestamp-drift.py`, `test-adapter.sh`, `test_authority`, `test_authority_fold`, `test_authority_fold10`, `test_authority_fold11`, `test_authority_fold12`, `test_authority_fold2`, `test_authority_fold3`, `test_authority_fold4`, `test_authority_fold5`, `test_authority_fold6`, `test_authority_fold7`, `test_authority_fold8`, `test_authority_fold9`, `test_cold_install`, `test_draft_shape`, `test_draft_shape_lint`, `test_e2e_chain`, `test_finding_tags`, `test_fixture_seam_copy`, `test_fold_contract`, `test_gate_review`, `test_history_audit`, `test_judging`, `test_lint_relay_root_record`, `test_merge_claim`, `test_merge_claim_corrections`, `test_mtbattery_guard`, `test_parity`, `test_plan_shape_warnings`, `test_root_lint_corrections`, `test_routed_notice`, `test_show_diagnostics`, `test_version`
- `tools/relay-lint-fixtures/**` → `check-fixture-readme-census.py`, `check-generate-plugins.py`, `check-relay-lint-fixtures.py`, `test-adapter.sh`, `test_authority_fold2`, `test_cold_install`, `test_draft_shape`, `test_draft_shape_lint`, `test_e2e_chain`, `test_finding_tags`, `test_fixture_seam_copy`, `test_fold_contract`, `test_gate_review`, `test_history_audit`, `test_human_gate`, `test_judging`, `test_judging_agreement`, `test_lint_relay_root_record`, `test_merge_claim`, `test_merge_claim_corrections`, `test_mtbattery_guard`, `test_parity`, `test_root_lint_corrections`, `test_routed_notice`, `test_show_diagnostics`, `test_version`
- `tools/relay-engine-fixtures/**` → `test_authority_fold2`, `test_bootstrap_tools`, `test_draft_shape`, `test_draft_shape_admission`, `test_draft_shape_lint`, `test_finding_tags`, `test_fixture_seam_copy`, `test_fold_contract`, `test_gate_review`, `test_history_audit`, `test_jcs`, `test_judging_agreement`, `test_lint_relay_root_record`, `test_merge_claim`, `test_merge_claim_corrections`, `test_migrate`, `test_parity`, `test_plan_contract`, `test_plan_shape_warnings`, `test_projections`, `test_root_lint_corrections`, `test_routed_notice`, `test_show_diagnostics`
- `tools/check-relay-lint-fixtures.py` → `check-fixture-readme-census.py`, `check-generate-plugins.py`, `test_fixture_seam_copy`, `test_mtbattery_guard`, `test_version`
- `tools/measure-plan-shape.py` → `test_plan_shape_warnings`

Notes on the entries:

- A new or removed engine module also changes the engine roster (`version.py` `ROSTER`) and `LOCKED_ENGINE_SET` in `check-generate-plugins.py`, which those tests compare.
- A new error code also joins `_CODE_VALUES` (`tools/relay_engine/strings.py`), the all-members list the string gate checks.
- Engine modules and `tools/adapters/` ship into `plugins/` (`generate-plugins.py` `ENGINE_SET` and `TOOLS_SET`), so a change there also needs regenerated plugins and `generate-plugins.py --check`.
- The `tools/**` row names the modules that copy the whole `tools/` tree, so they are not repeated on the other rows.
- `test_cold_install` reads `skills/` through `ruled_manifest`, which it imports from `test_e2e_chain`, and `test_version` reads `skills/`, `shared/`, `vendor/` and `LICENSE*` through the generator's `generate_tree`.
- The `tools/relay-lint-fixtures/**` and `tools/relay-lint.py` entries last until those files are deleted (DD-v3-tests stage 4).
- The kit pair adds the entry for `tools/kit_tests/` when that directory lands.
