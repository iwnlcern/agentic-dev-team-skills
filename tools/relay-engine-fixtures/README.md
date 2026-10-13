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
