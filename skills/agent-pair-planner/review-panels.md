# Adversarial Review Panels

Use the smallest panel that matches risk. Reviewers are **lenses**, not fixed model identities. If the host supports subagents or agent teams, spawn reviewers; otherwise run lenses sequentially and keep findings separated.

Default panels are baselines, not a prison. Agents may add, remove, merge, or swap reviewer roles when the risk shape calls for it, but must record:

```text
PANEL_CHOSEN: <team-of-2 | team-of-4 | team-of-5 | custom>
DEFAULT_ROLES_CHANGED: <yes/no>
WHY_THIS_PANEL: <risk/size/language/domain reason>
ROLES:
- <role> — <reason>
```

## Panel selection

- **Team of 2:** small, low-risk PRs or post-fold quick checks.
- **Team of 4:** default for non-trivial PRs.
- **Team of 5:** Team of 4 plus idiomaticity; default for C++/systems, high-risk, large refactors, or language-style-sensitive codebases.
- **Custom:** use when risk calls for migrations/RLS, product semantics, accessibility, observability, concurrency, release/deploy, or domain-owner review.

## Team of 2

1. **Correctness / design conformance** — locked plan, acceptance criteria, target entity, no dead controls, no writer-with-no-reader.
2. **Tests / coverage** — tests prove the behavior, correct layer asserted, no false-green risk.

## Team of 4

1. **Defensive security implications** — authz, tenant isolation, RLS, permissions, secrets, sessions, PII, injection, CSRF/CORS, unsafe deserialization, external integrations, destructive writes, migration/runtime-role safety. Do not provide exploit steps or offensive payloads.
2. **Performance impact** — N+1s, query plans, indexes, allocations, hot paths, latency, concurrency, locking, retries, idempotency, workers, queues, races, backpressure.
3. **Test coverage** — red/green proof, fixtures, false-green risks, privileged-test/runtime-role gaps, regression coverage, observability/audit proof.
4. **Correctness against design and implementation docs** — plan conformance, acceptance criteria, boundary contract, target-entity semantics, downstream consumers, out-of-scope list.

## Team of 5

Run Team of 4 plus:

5. **Code idiomaticity / language-standard conformance** — project idioms, modern syntax expectations, maintainability, agent-generated awkwardness, over-engineering, needless abstraction, nonlocal style drift.

For **C++**, explicitly check RAII, ownership/lifetimes, dangling references, `const` correctness, move/copy behavior, smart pointers, STL/ranges usage appropriate to the project standard, exception/error handling, undefined behavior risk, allocation patterns, header hygiene, ABI/API compatibility, and lint/format rules.

## Reviewer output

```text
Lens:
Verdict: <block | must-fix | optional | approve>
Evidence: <claim -> E0-E4 -> source>
Findings:
1. <severity> <file:line/behavior/evidence> — <issue> — <required change>
Boundary contract: <pass/fail/not applicable>
Live verification impact: <none | required | missing>
Optional fold-ins worth doing cheaply:
```

## Consolidation rules

- `block` prevents merge until fixed or human-overridden.
- `must-fix` should be folded before merge.
- `optional` is Implementer-discretion unless human-directed.
- Nits do not block.
- Findings without evidence become questions or are dropped.
- After fold-ins, run a targeted check. Do not rerun the full panel unless directed or the fold changes design/blast radius.
