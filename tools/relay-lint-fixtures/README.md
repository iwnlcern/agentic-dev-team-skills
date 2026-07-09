# relay-lint fixture matrix

These fixtures are the tool-correctness gate for `tools/relay-lint.py`. They cover cooperative/historical shapes, adversarial/evasive shapes, addressing, merge-claim lineage, content/substance checks, and P9 block-boundary regressions.

Expected outcomes:

```text
claude/A1-valid-audit.md                                               pass
claude/A2-valid-downgrade.md                                           pass
claude/B2-why-before-scan.md                                           fail
claude/B3-yes-row-why.md                                               fail
claude/B5-audit-no-final.md                                            fail
claude/B6-audit-impl-authority.md                                      fail
claude/B7-delegated-no-scopediff.md                                    fail
claude/B8-deviation-dispatch.md                                        fail
claude/B9-bad-enum.md                                                  fail
claude/C1-evasive-rows.md                                              fail
claude/C2-enum-bypass.md                                               fail
lint-test/bad1.md                                                      fail
lint-test/bad2.md                                                      fail
lint-test/bad3.md                                                      fail
lint-test/bad3b.md                                                     fail
lint-test/bad4.md                                                      fail
lint-test/good5.md                                                     pass
--relay-root claude/L1                                                 pass
--relay-root claude/L2                                                 pass
--relay-root claude/L3                                                 fail
probes/N2-refusal-code-span.md                                         pass
probes/N3-fenced-dispatch-inert.md                                     pass
probes/N4-scan-result-mismatch.md                                      fail
probes/N5-missing-scan-row.md                                          fail
addressing/T1-valid-addressed-dispatch.md                              pass
addressing/T2-token-no-to.md                                           fail
addressing/T3-token-to-planner.md                                      fail
addressing/T4-token-two-implementers.md                                fail
addressing/T5-bad-address.md                                           fail
addressing/T6-valid-audit-both-to.md                                   pass
addressing/T7-valid-cc-context.md                                      pass
addressing/T8-cc-trap-structurally-valid.md                            pass
--relay-root addressing/G1-casefold-lineage                            pass
--relay-root merge/M1-merge-claim-no-auth                              fail
--relay-root merge/M2-merge-claim-with-auth                            pass
--relay-root merge/M3-honest-not-merged                                pass
--relay-root merge/M4-canonical-claim-no-auth                          fail
--relay-root merge/M5-self-auth-forgery                                fail
--relay-root merge/M7-continuation-prose-claim-no-auth                 fail
--relay-root merge/M8-continuation-canonical-claim-no-auth             fail
--relay-root merge/BP5-blank-line-merge-evasion                        fail
--relay-root merge/GA-flushleft-canonical-no-auth                      fail
--relay-root merge/GB-tab-ambiguous-canonical                          fail
--relay-root merge/GC-2blank-ambiguous-canonical                       fail
--relay-root merge/GD-quoted-canonical-inert                           pass
--relay-root merge/organic-m2-unauthorized-merge                       fail
--relay-root merge-token/MT1-valid-token-grant                         pass
--relay-root merge-token/MT2-backticked-token-inert                    fail
merge-token/MT3-self-granted-token.md                                  fail
merge-token/MT4-multi-to-grant.md                                      fail
merge-token/MT5-wrong-phase-token.md                                   fail
--relay-root merge-token/MT6-token-outside-root                        fail
--relay-root merge-token/MT7-duplicate-authorization-decoy              fail
--relay-root merge-token/MT8-denied-only-no-auth                        fail
--relay-root merge-token/MT9-cross-dispatch-runroot                     fail
content/E1-empty-final-git-status.md                                   fail
content/E2-empty-actions-git-ref.md                                    fail
content/E3-empty-scopediff-live-dispatch.md                            fail
content/E4-structured-unavailable.md                                   pass
content/E5-clean-tree.md                                               pass
content/E6-out-row-allin.md                                            fail
content/E7-valid-scopediff-dispatch.md                                 pass
content/E8-structured-none-scopediff.md                                fail
content/E9-unparseable-row.md                                          fail
content/E10-bare-unavailable.md                                        fail
content/E11-placeholder-field.md                                       fail
content/E12-colon-rows-valid.md                                        pass
content/E13-na-marker.md                                               fail
content/E13-scopediff-detached-row.md                                  fail
content/E14-scopediff-row-after-result.md                              fail
content/E15-dual-scopediff-decoy.md                                    fail
content/E16-dual-scopediff-contiguous.md                               fail
fold/FD1-fold-edit-no-foldscope.md                                     fail
fold/FD2-valid-fold-report.md                                          pass
fold/FD3-out-row-with-edit.md                                          fail
fold/FD4-empty-foldscope.md                                            fail
fold/FD5-deviation-relay-out-rows-no-edit.md                           pass
fold/FD6-edit-then-waiver-shape.md                                     fail
fold/FD7-fold-no-deviation-uncommitted.md                              fail
fold/FD8-scope-after-actions.md                                        fail
fold/FD9-fold-no-wordlist-verbs.md                                     fail
fold/FD10-detached-row-foldscope.md                                    fail
fold/FD11-row-after-result-foldscope.md                                fail
fold/FD12-absence-prefixed-actions.md                                  fail
fold/FD13-dual-foldscope-contiguous.md                                 fail
--relay-root p9/P9-blank-line-prose-after-block                        pass
p9/P9b-claim-after-scan-blank-line.md                                  fail
```

The linter must produce exit code 0 for pass fixtures and exit code 1 for fail fixtures. Exit code 2 is reserved for usage/internal errors.

Run:

```bash
python3 tools/check-relay-lint-fixtures.py
```

The helper compares observed exit codes to the expected-outcome table. Selected fixtures also assert complete sorted error sets, so fail fixtures must fail for the intended reason. The mapping name is `EXPECTED_ERROR_SET` even when the helper is used for single-file fixtures.

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

- MT1 proves a live `DISPATCH MERGE` grant from an authorized grantor to exactly one implementer authorizes a later merge claim.
- MT2 proves a backticked token is inert; the later merge claim remains unauthorized.
- MT3 proves implementer self-grants are structural errors.
- MT4 proves multi-implementer `TO` grants are invalid.
- MT5 proves a bare token outside `PHASE: MERGE-GATE` is a structural error.
- MT6 proves a token-bearing file outside the active relay root does not authorize an in-root merge claim.

M2 remains the layered field-form positive: field-form merge grants still authorize without the token.

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

```text
--relay-root design-review/DR1-valid-design-doc-chain             pass
--relay-root design-review/DR2-edge-less-no-review                fail
--relay-root design-review/DR3-direct-override                    pass
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




## Orchestrator-review visibility fixtures

The orchestrator-review visibility gate fixtures under `orch-review/`. They prove the broad SET, the operator-authored waiver path, the self-waiver rejection, the pair-Planner exemption, boot/SITREP exemption, operator exemption, and reviewer-boot shape. The gate is a visibility/addressing check only; it does not add an approval verdict or lineage walk.

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
--relay-root orch-review/A2-operator-waiver           pass
--relay-root orch-review/A3-self-waiver               fail
--relay-root orch-review/A4-solo-no-orchestrator-relay pass
--relay-root orch-review/EX1-operator-authority       pass
--relay-root orch-review/EX2-pairplanner-impl         pass
--relay-root orch-review/EX3-reviewer-boot            pass
--relay-root orch-review/EX4-reconcile                pass
--relay-root orch-review/EX5-proceed-nodeleg          pass
--relay-root orch-review/EX6-boot-to-reviewer         pass
```


