# Relay engine fixtures

The `record-root/` members exercise engine-root record-scoped lint behavior.
Imported/adopted rows are suppressed without ever having been fully linted; filed history is not relitigated.

The `judging/base-order-cbc8f07.json` capture preserves unsorted diagnostics from the base commit and its frozen fixture population.
It is independent of the regenerated parity golden.

The `judging/aligned/` fixtures compare every Finding field against the sweep for aligned admission order.
The `judging/future-sensitive/` fixtures hold the six named temporal cases and explicit admission and sweep expectations.
The lock-lifecycle and commission-surface cases require late ingestion of older-stamped evidence, so the required order-divergent precedence classifies those two as order-divergent despite their storage directory.
The `judging/order-divergent/` fixture covers a dispatch ingested before its older-stamped parent.
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
