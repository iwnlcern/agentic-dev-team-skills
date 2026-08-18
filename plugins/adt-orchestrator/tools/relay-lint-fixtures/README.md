# relay-lint fixture matrix

These fixtures are the tool-correctness gate for `tools/relay-lint.py`.
They cover cooperative/historical shapes, adversarial/evasive shapes, addressing, merge-claim lineage, content/substance checks, block-boundary regressions, design-doc and plan lock digests, role-vocabulary enforcement, INDEX root resolution, and the master-tier authority chains.

## Family census

The census below is the completeness contract for this corpus.
It is derived from the harness, not from this file: `tools/check-fixture-readme-census.py` re-runs `tools/check-relay-lint-fixtures.py`, counts the asserted outcomes per family, and fails when this table disagrees with the harness in any family, count, or total.
Update the table by re-deriving it from the harness output, never by editing rows in place.

| Family | Outcomes | What it gates |
|---|---|---|
| `addressing/` | 9 | FROM/TO/CC addressing; a live dispatch token must address exactly one implementer |
| `ambiguity/` | 5 | Resolver precision when unrelated relays reuse an identifier |
| `claude/` | 14 | Original cooperative/historical shapes plus enum and evasion probes |
| `content/` | 17 | Substance checks: empty/placeholder fields, SCOPE_DIFF contiguity and duplication |
| `design-review/` | 24 | DESIGN → DESIGN-REVIEW → gated PLAN lineage, including shared-thread resolver precision |
| `fold/` | 13 | REVIEW-FOLD scope artifacts: FOLD_SCOPE blocks, OUT rows, ordering |
| `hardening/` | 13 | Hardened field and block-boundary edges: downgrades, detached rows, filled-template and prose-delegation forms |
| `humangate/` | 4 | HUMAN_GATE_REQUIRED annotation discipline: bare/separator-only `yes` warns, annotated `yes` and `no` forms stay silent |
| `identity/` | 1 | ROLE/FROM proxy-authoring tripwire |
| `indexmarker/` | 3 | INDEX `root:` marker mechanics: placement and duplicate refusal |
| `kr8a/` | 4 | KR8A carrier exemption: quoted work claims in carriers versus live tokens |
| `lineage/` | 7 | PARENT_DISPATCH_ID parent chains for pair-Planner dispatch and IMPL reports |
| `lint-test/` | 6 | Basic pass/fail smoke shapes |
| `lockdigest/` | 63 | PLAN/DESIGN SHA-256 lock digests: float-forward, rollback, malformed and conflicting digest groups, fence and boundary evasions |
| `lockpath/` | 4 | PLAN_LOCK_ID grammar: old path form, new form, bare id, missing |
| `mastertier/` | 316 | Master-tier authority chains: charters, commissions, grants, receipts, seat and parent legitimacy |
| `merge/` | 13 | Merge claims: authorization, canonical machine forms, continuation and whitespace evasions |
| `merge-token/` | 9 | Merge-token grants: phase binding, grantor and addressee restrictions, inert quoted forms |
| `orch-review/` | 22 | Orchestrator-reviewer visibility gate over the broad SET, waivers, and exemptions |
| `p9/` | 2 | P9 block-boundary regressions |
| `probes/` | 4 | Adversarial/evasive probes: code spans, fenced tokens, scan-result mismatches |
| `rolevocab/` | 61 | Role-vocabulary enforcement: v2.9 renames, leftover roles, multiholder and late-binding edges |
| `rootindex/` | 2 | Root INDEX ordering |
| `rootres/` | 15 | INDEX root resolution: markers, file cells, duplicates, near-misses |
| `rowtruth/` | 5 | ROW_TRUTH_CHECK evidence hooks for FOLD_SCOPE and SCOPE_DIFF rows |
| `xroot/` | 109 | Declared cross-root design edge: both axes, fail-closed, plan-path symmetry; generated at harness runtime by tools/xrootfixgen.py |

**745 harness-asserted outcomes** across 26 families.

Two notes on the corpus shape.
An outcome is one harness assertion, not one file: the lineage-bearing families (`mastertier/`, `design-review/`, `lockdigest/`, and others) stage whole relay-root scenarios — several relay files plus an INDEX per case — because the rules under test judge chains, not single relays.
The `t11closure/` fixture tree that also lives in this directory is exercised by its own harness, `tools/check-t11-closure.py`, and is deliberately absent from this census.

## Run

```bash
python3 tools/check-relay-lint-fixtures.py
```

The linter must produce exit code 0 for pass fixtures and exit code 1 for fail fixtures. Exit code 2 is reserved for usage/internal errors.

The helper compares observed exit codes to its expected-outcome tables. Selected fixtures also assert complete sorted error sets, so fail fixtures must fail for the intended reason. The mapping name is `EXPECTED_ERROR_SET` even when the helper is used for single-file fixtures.

The narrative sections below explain the families whose design needs prose; the remaining families are self-describing through their case names and the harness tables.

## Permanent merge-block fixtures

Additional merge-block fixtures remain permanent:

```text
--relay-root merge/M7-continuation-prose-claim-no-auth       fail
--relay-root merge/M8-continuation-canonical-claim-no-auth   fail
--relay-root merge/organic-m2-unauthorized-merge             fail with exactly four declared errors
```

The organic m2 fixture is retained permanently because it is the real unauthorized-merge artifact shape that motivated the check. Synthetic fixtures verify the spec; organic fixtures verify what agents actually write. The organic fixture is asserted per error, not merely by exit code.

The content/substance fixtures E1-E13 plus P9/P9b, BP5, and GA/GB/GC. The P9 block-boundary fixtures verify that field blocks end at the first blank line or next ALL-CAPS header; BP5 verifies that an indented continuation-looking line after a blank-closed `ACTIONS_GIT_REF` block fails closed. GA verifies that flush-left canonical `merge=<sha>` machine forms are still caught in relay-root lineage mode; GB/GC lock tab and double-blank ambiguity edges; GD proves backticked canonical examples are inert.

## FOLD_SCOPE and content fixtures

These row-bearing FOLD_SCOPE, detached-row, and duplicate row-bearing block regression fixtures.

- `content/E13-scopediff-detached-row.md` and `content/E14-scopediff-row-after-result.md` preserve the two SCOPE_DIFF contiguity bypass shapes from the plan packet.
- `content/E15-dual-scopediff-decoy.md` and `content/E16-dual-scopediff-contiguous.md` are verbatim ports of the r1 Pd/Pe probes. They verify duplicate `SCOPE_DIFF` / `SCOPE_DIFF_RESULT` blocks are structural errors; E15 also preserves the detached OUT-row window over a second block.
- `fold/FD1`-`FD12` exercise REVIEW-FOLD scope artifacts: no scope, valid fold, OUT rows, empty scope, deviation/no-edit pass, edit-then-waiver, uncommitted out-of-scope edit, scope-after-actions ordering, action-record without wordlist verbs, detached FOLD_SCOPE rows, row after `FOLD_SCOPE_RESULT`, and absence-prefixed action records.
- `fold/FD13-dual-foldscope-contiguous.md` is the FOLD_SCOPE twin of the duplicate-block class.
- `fold/FD6-edit-then-waiver-shape.md` keeps the organic “fixed …” prose and its gate fires through substantive `ACTIONS_GIT_REF`, not the old wordlist/boilerplate accident.

## Merge-token fixtures

Merge-token fixtures MT1-MT6:

- MT1 currently fails before the token-grant assertion: its operator-authored MERGE-GATE declares `ROLE: Orchestrator Planner`, so the ROLE/FROM anti-proxy check rejects it. Its bytes therefore do not currently demonstrate a valid token grant.
- MT2 proves a backticked token is inert; the later merge claim remains unauthorized.
- MT3 proves implementer self-grants are structural errors.
- MT4 proves multi-implementer `TO` grants are invalid.
- MT5 proves a bare token outside `PHASE: MERGE-GATE` is a structural error.
- MT6 proves a token-bearing file outside the active relay root does not authorize an in-root merge claim.

M2 currently fails before the layered field-form assertion: its operator-authored MERGE-GATE declares `ROLE: Planner`, so the ROLE/FROM anti-proxy check rejects it. Its bytes therefore do not currently demonstrate that field-form grants authorize without the token.

## Merge-authorization and lineage-scope fixtures

Merge-authorization and lineage-scope fixtures MT7-MT9:

- MT7 ports the duplicate/conflicting authorization decoy: one MERGE-GATE relay contains a denial plus a later approving line. The grant is structurally dirty and does not authorize the merge claim.
- MT8 locks the denied-only path: a single denial line is not duplicate/conflicting, but it also does not authorize the later merge claim.
- MT9 locks merge lineage by `DISPATCH_ID`: a valid grant in a sibling dispatch under the same run root does not authorize a claim with a different `DISPATCH_ID`.

## Identity, lineage, and row-truth fixtures

Identity, lineage, and row-truth fixtures:

- `identity/S4a-proxy-from.md` preserves the proxy-FROM confusion repro: a relay whose `ROLE` does not match the non-special role suffix in `FROM` is structurally dirty.
- `lineage/LI1-valid-parent-chain` is the valid parent-chain shape: pair Planner PLAN -> Implementer PLAN-REVIEW approve -> pair Planner DISPATCH IMPL.
- `lineage/LI2-cc-orchestrator-plan-trap` fails when an Implementer approves a CC'd orchestrator PLAN and the pair Planner dispatches against that review.
- `lineage/LI3-no-plan-review-parent` fails when a pair Planner dispatch points to a PLAN instead of an approving PLAN-REVIEW.
- `lineage/LI4-non-addressee-impl-report` fails when an Implementer report claims actions but the parent dispatch was addressed to another Implementer.
- `rowtruth/RT1` and `RT3` are clean row-truth evidence hooks for FOLD_SCOPE and SCOPE_DIFF.
- `rowtruth/RT2` and `RT4` fail when `ROW_TRUTH_CHECK: required` is present but IN rows lack matching evidence.
- `rowtruth/RT5-two-relay-out-in-flip` fails when a path flips from OUT to IN across the same dispatch lineage under `ROW_TRUTH_CHECK`.

Edge-less observed-shape fixtures for the parent-lineage gate:

- `lineage/LI5-edgeless-delegated-cc-trap` fails when a delegated pair-Planner dispatch omits `PARENT_DISPATCH_ID`; the edge is mandatory, not opt-in.
- `lineage/LI6-edgeless-no-approve` fails when a pair-Planner dispatch has no parent edge and no approving review chain.
- `lineage/LI7-edgeless-non-addressee-report` fails when an IMPL report claims substantive implementation actions without parenting to the addressed dispatch.

## Design-review fixtures

The design-review fixture matrix is DR1-DR11 plus DR13-DR15. DR12 is intentionally held out for the non-builder lane.

DR3 currently fails before the direct-override assertion: its operator-authored
PLAN declares `ROLE: Reviewer`, so the ROLE/FROM anti-proxy check rejects it.
Its bytes therefore do not currently demonstrate a valid direct override.

```text
--relay-root design-review/DR1-valid-design-doc-chain             pass
--relay-root design-review/DR2-edge-less-no-review                fail
--relay-root design-review/DR3-direct-override                    fail
--relay-root design-review/DR4-self-override                      fail
--relay-root design-review/DR5-tiny-no-lock                       pass
--relay-root design-review/DR6-genuine-audit-record               pass
--relay-root design-review/DR7a-audit-record-no-design-doc         pass
--relay-root design-review/DR7b-audit-record-design-doc-visible    fail
--relay-root design-review/DR8-review-not-from-implementer         fail
--relay-root design-review/DR9-verdict-not-approve                 fail
--relay-root design-review/DR10-review-parent-not-design           fail
--relay-root design-review/DR11a-stale-review-parent               fail
--relay-root design-review/DR11b-locks-must-revise-v1              fail
--relay-root design-review/DR11c-positive-control-v2               pass
--relay-root design-review/DR13-omit-record-kind         fail
--relay-root design-review/DR14-legacy-lock-tolerated              pass
--relay-root design-review/DR15-verdict-human-decision          fail
```

## Shared-thread lineage fixtures

Seven design-review lineage fixtures for shared-thread `DISPATCH_ID` resolver precision. They prove the linter selects same-owner, role/phase/doc-aware DESIGN-REVIEW and DESIGN parents rather than the earliest relay sharing an id.

```text
--relay-root design-review/DR16-F-a-same-id-orch-design        pass
--relay-root design-review/DR17-F-relock-latest-before-review  pass
--relay-root design-review/DR18-F-thread-one-id-pass           pass
--relay-root design-review/DR19-F-thread-mustrevise            fail
--relay-root design-review/DR20-F-b-no-design-parent           fail
--relay-root design-review/DR21-F-b158-no-owner-review         fail
--relay-root design-review/DR22-F-c-cross-owner-design         fail
```

## Ambiguity fixtures

AMB1a and AMB1b exercise resolver precision when unrelated relays reuse an
identifier. AMB1c is currently a stale, pre-resolution ROLE/FROM failure case.

```text
--relay-root ambiguity/AMB1a-shared-id-dispatch       pass
--relay-root ambiguity/AMB1b-shared-id-planhop        pass
--relay-root ambiguity/AMB1c-shared-id-implreport     fail
```

AMB1a and AMB1b are valid shared-ID controls. AMB1c currently fails before the IMPL-report parent-resolution assertion: its operator-authored dispatch declares `ROLE: Planner`, so the ROLE/FROM anti-proxy check rejects that dispatch.

## Orchestrator-review visibility fixtures

The orchestrator-review visibility gate fixtures under `orch-review/` cover the broad SET, self-waiver rejection, pair-Planner exemption, boot/SITREP exemption, and reviewer-boot shape. A2 and EX1 are intended to cover the operator-authored waiver and operator exemption, but their current bytes fail the earlier ROLE/FROM anti-proxy check as detailed below. The gate is a visibility/addressing check only; it does not add an approval verdict or lineage walk.

```text
--relay-root orch-review/OR1-audit-cc                 pass
--relay-root orch-review/OR2-design-cc                pass
--relay-root orch-review/OR3-plan-delegated-cc        pass
--relay-root orch-review/OR4-override-impl-cc         pass
--relay-root orch-review/OR5-merge-gate-cc            pass
--relay-root orch-review/OR6-review-fold-cc           pass
--relay-root orch-review/OR7-audit-no-reviewer        fail
--relay-root orch-review/OR8-design-no-reviewer       fail
--relay-root orch-review/OR9-plan-delegated-no-reviewer fail
--relay-root orch-review/OR10-override-impl-no-reviewer fail
--relay-root orch-review/OR11-merge-gate-no-reviewer  fail
--relay-root orch-review/OR12-review-fold-no-reviewer fail
--relay-root orch-review/A1-no-reviewer-no-waiver     fail
--relay-root orch-review/A2-operator-waiver           fail
--relay-root orch-review/A3-self-waiver               fail
--relay-root orch-review/A4-solo-no-orchestrator-relay pass
--relay-root orch-review/EX1-operator-authority       fail
--relay-root orch-review/EX2-pairplanner-impl         pass
--relay-root orch-review/EX3-reviewer-boot            pass
--relay-root orch-review/EX4-reconcile                pass
--relay-root orch-review/EX5-proceed-nodeleg          pass
--relay-root orch-review/EX6-boot-to-reviewer         pass
```

A2 and EX1 currently fail before their waiver/operator-authority assertions: the operator-authored relay in each fixture declares `ROLE: Reviewer`, so the ROLE/FROM anti-proxy check rejects it. Their fixture bytes therefore do not currently demonstrate those positive paths.
