# Relay engine fixtures

The `record-root/` members exercise engine-root record-scoped lint behavior.
Imported/adopted rows are suppressed without ever having been fully linted; filed history is not relitigated.

The `judging/base-order-cbc8f07.json` capture preserves unsorted diagnostics from the frozen base source against the moved fixture population, in the capture host's traversal order.
It is independent of the regenerated parity golden.
After the omitted inputs' diagnostics are removed, the re-capture reorders 253 remaining positions: 63 errors and 190 warnings.
The remaining diagnostic multisets are unchanged; the original capture also does not reproduce its exact traversal order on this host.
This host-dependent ordering change is separate from the eight README errors and one LP2 warning removed with their inputs, and from the three downstream insertion-offset changes.
The capture command, source/population pins, before/after digests and independent order comparison are retained in the Task 13 review evidence.

The `lint-population/` tree is the shipped v2 linter's fixture tree as at `origin/v3` `2b4f83e`, copied byte for byte when the engine's tests stopped reading the linter's own tree (plan Task 13).
It omits that tree's README and the `lockpath/LP2-new-form/` case, the three files that name the linter.
`test_judging` and `test_judging_agreement` read the copied population; `test_human_gate` reads its `humangate/` cases.
The order check in `test_finding_tags` and `test_history_audit` reads it without the two `merge-token` cases added after `cbc8f07`, so its population is that commit's tree less the same three files.

The `draft-shape/wrapper-messages-2b4f83e.json` fixture freezes all 1,005 diagnostic-input hashes and independent expected wrapper messages from the pinned base diagnostic oracle.
It records the producing source commit and digest, plus canonical key/entity table digests; runtime tests do not load that producer.
The canonical table form is ASCII JSON with sorted keys and compact separators, covering every key and value.

The `judging/aligned/` fixtures compare every Finding field against the sweep for aligned admission order, including gate-review approval, no-reply, and a SITREP admitted after an unheard grant.
The `judging/future-sensitive/` fixtures hold the six named temporal cases and explicit admission and sweep expectations.
The lock-lifecycle and commission-surface cases require late ingestion of older-stamped evidence, so the required order-divergent precedence classifies those two as order-divergent despite their storage directory.
The `judging/order-divergent/` fixtures cover a dispatch ingested before its older-stamped parent and a gate-review reply whose sweep position precedes its request despite later admission.
Fixture partition flags describe the actual input ordering, not a directory-name oracle.

The `record-root/history-only/` fixture holds recorded lineage defects and their frozen cbc8f07 sweep diagnostics.
Default managed sweeps suppress those defects; history audits report the exact frozen messages as warnings.

## B9 cross-root baseline

`xroot/baseline-b8d87eb.json` records measured pair and direct-issuer authority reason sets, verifier axes/details and successful selections at `b8d87ebe15fbd3f5e06f1d0112fccad832acd1b1`.
Its source hashes and capture interpreter/load records bind the results to the old implementation.
Each case was measured once per path; the transition and supplemental differential captures cover disjoint inputs.
`relay_engine/tests/capture_xroot_baseline.py` loads explicit pinned source files and `relay_engine/tests/xroot_b9_cases.py` builds the foreign repositories.
Deadline fault cases expire the selected real Git query; capacity shapes use the real five-second budget and the OS page cache is uncontrolled.
These old measurements are a differential oracle, not proof of B9 performance.

`xroot/baseline-b8d87eb-lifted.json` is the separate revision-4 oracle for the four natural bounded timeouts: B1-complete-within-envelope, C1-over-count-20001, F-depth-2500 and M1-over-trees-20001.
The `--lift-budget` harness loads the same pinned old source bytes, lifts their internal budget to 600 seconds, injects no fault, and retains each original bounded answer beside its lifted measurement.
Injected deadline cases T1, T1b, T2 and T3 remain bounded controls and have no lifted entry.
Declared changed rows take precedence over both captures; other natural-timeout rows use the lifted structural answer, and all remaining rows use the bounded answer.
Neither committed capture may be overwritten or retried to obtain a preferred result.

`xroot/baseline-b8d87eb-narrow.json` retains 23 old-reader narrow-read controls captured from an isolated b8d87eb worktree with a lifted 600-second budget.
The capture covers candidate and noncandidate hygiene forms, directory-position changes, an ordinary directory containing a candidate, and structural faults.
The original bounded and lifted captures remain unchanged.

`xroot/baseline-b8d87eb-corpus.json` records the old verifier's own answers over 130 shared cases and 43 synthetic cases at the same pinned commit.
It uses a 600-second diagnostic budget except for the shared FIFO deadline control, which keeps five seconds.
Population bodies are represented by SHA-256 hashes; names, parsed fields, timestamps, selected origin and review, and refusal axes and details remain intact.
The capture records the SHA-256 of the complete raw producer output retained as `baseline-b8d87eb-corpus-raw.json` in the B9 Task 2n evidence.
The corpus test compares old-reader answers only within the explicitly classified old-reader domain, with the named established budget, peer and envelope changes stated in the test.
The independent serial-reference comparison covers every corpus case without those exceptions and compares complete retained text and both authority paths.
