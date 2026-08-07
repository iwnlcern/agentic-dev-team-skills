# Reviewer Spawn Prompts

Copy-paste these for Claude Code-style agent teams, subagents, or equivalent background workers. Reviewers are read-only unless explicitly reassigned.

`HUMAN_GATE_REQUIRED: yes` if and only if the current relay's requested next transition cannot occur without a fresh operator decision. A `yes` names its ask in the annotation (`yes — <the decision>`); a bare `yes` is malformed. Standing downstream gates are named only after `downstream:` or in prose, never in the enum value. The field is a predicate re-evaluated at each relay, not a latch.

## Shared review context

```text
Shared review context:
PR / branch: <repo#PR or branch>
DISPATCH_ID: <id>
DESIGN_LOCK_ID: <logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
PLAN_LOCK_ID: <logical plan id>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
Locked design / plan: <path or summary>
Acceptance criteria:
1. <criterion>
Boundary contract:
- Writes:
- Reads:
- Target entity:
- Downstream consumer:
- Contract:
Out of scope:
- <files/behaviors>
Tests already run:
- <command -> result>
Evidence target: <E2 | E3 | E4>
Reviewer rules:
- Read-only review only.
- Cite file:line, diff hunk, command/test output, or runtime evidence.
- Label evidence E0-E4.
- Return block/must-fix/optional/approve.
- Convert weak claims into questions.
```

## Claude Code agent team — Team of 4

```text
Create an agent team to review <PR/branch>. Spawn four read-only reviewers:

1. security-reviewer — defensive security implications: authz, tenant isolation, RLS, permissions, sessions, secrets, PII, injection, external integrations, destructive writes, runtime-role safety. Do not provide exploit steps or offensive payloads.
2. performance-reviewer — N+1s, query/index behavior, hot paths, allocations, concurrency, locks, workers, queues, idempotency, retries, race conditions, backpressure.
3. test-coverage-reviewer — whether tests prove acceptance criteria, missing fixtures, false-green risks, runtime-role gaps, regression coverage, observability/audit proof.
4. correctness-reviewer — locked design/plan conformance, acceptance criteria, boundary contract, target-entity semantics, downstream consumers, out-of-scope boundaries.

Use the shared review context below. Require each reviewer to return Lens, Verdict, Evidence, Findings, Boundary contract, and Live verification impact. Synthesize blockers, must-fixes, optional fold-ins, rejected/weak findings, and whether REVIEW-FOLD is required.

<PASTE SHARED REVIEW CONTEXT>
```

## Claude Code agent team — Team of 5 / C++ or idiomaticity-sensitive

```text
Create an agent team to review <PR/branch>. Spawn the Team of 4 plus:

5. idiomaticity-reviewer — code idiomaticity and language-standard conformance. For C++, check RAII, ownership/lifetimes, const correctness, move/copy behavior, smart pointers, STL/ranges usage appropriate to the configured standard, exception/error handling, undefined behavior risk, allocation patterns, header hygiene, ABI/API compatibility, and project lint/format rules.

Use the shared review context below. Read-only review only. Label evidence E0-E4. Synthesize blockers, must-fixes, optional fold-ins, and whether REVIEW-FOLD is required.

<PASTE SHARED REVIEW CONTEXT>
```

## Custom panel

```text
Create a read-only review panel for <PR/branch> because <risk reason>.

Reviewers:
- <name> — <lens + why>
- <name> — <lens + why>

Default Team-of-4 changes:
- Removed/merged: <role + why safe>
- Added/swapped: <role + risk covered>

Each reviewer must cite evidence E0-E4 and return block/must-fix/optional/approve. Synthesize after all finish.

<PASTE SHARED REVIEW CONTEXT>
```

## Lead synthesis prompt

```text
Synthesize reviewer findings for <PR/branch>.

Group by blocker, must-fix, optional, nit, rejected/weak. Preserve lens and evidence level. Drop duplicates after keeping strongest evidence. Convert E0-only findings into questions unless they require follow-up for safety. Mark what the Implementer must fold before merge. End with one verdict: <REVIEW-FOLD required | quick-check only | merge-gate ready | human decision required>.
```
