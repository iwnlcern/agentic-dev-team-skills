# v29 variants implementation verification

## Evidence identity

Worktree: `/private/tmp/agentic-dev-team-skills-v29-variants-impl`.

BASE_SHA: `77e45871eba6da432a7adf8a3ea647438288e60a`.

TESTED_SHA: `c8b90d20b636259247fcb9f1370e25a04723a3d5`.

The commands and literal outputs recorded below are the Task 4 E2 evidence for the immutable tested bytes.

## Claim evidence levels

| Claim | E-level | Proof |
|---|---|---|
| Clean immutable target at TESTED_SHA. | E2 | `git rev-parse HEAD` and `git status --short`. |
| Adapter behavioral, fallback, registration, and ladder checks pass. | E2 | `bash tools/adapters/claude-code/test-adapter.sh`. |
| Generated plugin files match their canonical source. | E2 | `python3 tools/generate-plugins.py --check`. |
| Relay-lint and timestamp fixture corpora pass. | E2 | The two fixture commands below. |
| Root AGENTS.md is unique. | E2 | The find census below. |
| The two hook fallback ladders are non-empty and byte-identical. | E2 | The awk and cmp proof below. |
| Deprecated Codex-root occurrences within `tools/` `*.sh`/`*.md`, fixtures excluded, are fully classified. | E2 | The two censuses below. |
| AGENTS.md and the named production surfaces exist. | E1 | Their successful file-presence reads in the find and named-surface census. |

## Command proofs and literal verdict output

### Target and clean tree

```sh
git rev-parse HEAD
git status --short
```

```text
c8b90d20b636259247fcb9f1370e25a04723a3d5
```

Exit status: 0.

`git status --short` produced no output.

### Adapter gate

```sh
bash tools/adapters/claude-code/test-adapter.sh
```

Exit status: 0.

```text
PASS v1-write-visible-relay-root
PASS v1-bash-visible-relay-root
PASS v2-write-visible-index-direct-root
PASS v2-bash-visible-index-direct-root
PASS v2-write-visible-index-nested
PASS v2-bash-visible-index-nested
PASS v3-write-relays-near-miss
PASS v3-bash-relays-near-miss
PASS v4-write-hidden-relay-root
PASS v4-bash-hidden-relay-root
PASS v5-write-hidden-index
PASS v5-bash-hidden-index
PASS a-clean-relay
PASS b-dirty-fd1
PASS b2-dirty-e1-tripwire
PASS c-non-relay-path
PASS c2-non-md-relay-root
PASS c3-empty-index-md
PASS i1-valid-index-md
PASS i2-decreasing-index-row
PASS i3-raw-split-index-row
PASS d-no-linter
PASS e-broken-linter
PASS g1-literal-relay-redirect
PASS g2-non-relay-redirect
PASS g3-index-heredoc-routes-index-mode
PASS g4-mv-to-relay-directory-is-generic
PASS g5-interpreter-one-liner-is-residual
PASS g6-background-write-is-generic
PASS g7-write-then-false-failure-event
PASS g8-multiple-relay-targets-are-generic
PASS g9-notindex-routes-explicit-file-mode
PASS s1-shipped-bash-registration-both-events
PASS f0-write-confroot-beats-agents
PASS f0-bash-confroot-beats-agents
PASS f1-write-agents-beats-deprecated
PASS f1-bash-agents-beats-deprecated
PASS f2-write-deprecated-still-resolves
PASS f2-bash-deprecated-still-resolves
PASS f3-write-deprecated-beats-path
PASS f3-bash-deprecated-beats-path
PASS f4-write-path-only-resolves
PASS f4-bash-path-only-resolves
PASS l1-ladder-byte-identity
```

### Generated-plugin gate

```sh
python3 tools/generate-plugins.py --check
```

Exit status: 0.

Literal output: empty.

### Relay-lint fixture gate

```sh
python3 tools/check-relay-lint-fixtures.py
```

Exit status: 0.

Literal output:

The block below is a verbatim recapture from a fresh successful run at fold time.

```text
rolevocab/RV1a-pair-planner.md: expected=0 observed=0 PASS
rolevocab/RV1b-pair-implementer.md: expected=0 observed=0 PASS
rolevocab/RV2a-master-planner.md: expected=0 observed=0 PASS
rolevocab/RV2b-master-reviewer.md: expected=0 observed=0 PASS
rolevocab/RV3a-domain-planner.md: expected=0 observed=0 PASS
rolevocab/RV3b-domain-reviewer.md: expected=0 observed=0 PASS
rolevocab/RV4-invalid-role.md: expected=1 observed=1 PASS
rolevocab/RV5-planner-from-pair-planner.md: expected=0 observed=0 PASS
rolevocab/RV6-pair-implementer-from-implementer.md: expected=0 observed=0 PASS
rolevocab/RV7a-master-dispatch-failclosed.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
rolevocab/RV7b-domain-direct-override.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RV5-new-vocab-chain: expected=0 observed=0 PASS
--relay-root rolevocab/RV5d-newvocab-designdoc-chain: expected=0 observed=0 PASS
--relay-root rolevocab/RV5dneg-designdoc-no-approve: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RV6-mixed-vocab-chain: expected=0 observed=0 PASS
--relay-root rolevocab/RV6i-mixed-dispatch-implreport: expected=0 observed=0 PASS
--relay-root rolevocab/RV7a-master-dispatch-failclosed: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RV7b-domain-direct-override: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RV7c-master-merge-grant: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RV7d-master-broadset-no-fire: expected=0 observed=0 PASS
--relay-root rolevocab/RVn1-cross-owner-review: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RVn2-wrong-to: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RVn3-non-addressee-implreport: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root rolevocab/RVn4-cross-role-implreport: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
claude/A1-valid-audit.md: expected=0 observed=0 PASS
claude/A2-valid-downgrade.md: expected=0 observed=0 PASS
claude/B2-why-before-scan.md: expected=1 observed=1 PASS
claude/B3-yes-row-why.md: expected=1 observed=1 PASS
claude/B5-audit-no-final.md: expected=1 observed=1 PASS
claude/B6-audit-impl-authority.md: expected=1 observed=1 PASS
claude/B7-delegated-no-scopediff.md: expected=1 observed=1 PASS
claude/B8-deviation-dispatch.md: expected=1 observed=1 PASS
claude/B9-bad-enum.md: expected=1 observed=1 PASS
claude/C1-evasive-rows.md: expected=1 observed=1 PASS
claude/C2-enum-bypass.md: expected=1 observed=1 PASS
lint-test/bad1.md: expected=1 observed=1 PASS
lint-test/bad2.md: expected=1 observed=1 PASS
lint-test/bad3.md: expected=1 observed=1 PASS
lint-test/bad3b.md: expected=1 observed=1 PASS
lint-test/bad4.md: expected=1 observed=1 PASS
lint-test/good5.md: expected=0 observed=0 PASS
--relay-root claude/L1: expected=0 observed=0 PASS
--relay-root claude/L2: expected=0 observed=0 PASS
--relay-root claude/L3: expected=1 observed=1 PASS
probes/N2-refusal-code-span.md: expected=0 observed=0 PASS
probes/N3-fenced-dispatch-inert.md: expected=0 observed=0 PASS
probes/N4-scan-result-mismatch.md: expected=1 observed=1 PASS
probes/N5-missing-scan-row.md: expected=1 observed=1 PASS
addressing/T1-valid-addressed-dispatch.md: expected=0 observed=0 PASS
addressing/T2-token-no-to.md: expected=1 observed=1 PASS
addressing/T3-token-to-planner.md: expected=1 observed=1 PASS
addressing/T4-token-two-implementers.md: expected=1 observed=1 PASS
addressing/T5-bad-address.md: expected=1 observed=1 PASS
addressing/T6-valid-audit-both-to.md: expected=0 observed=0 PASS
addressing/T7-valid-cc-context.md: expected=0 observed=0 PASS
addressing/T8-cc-trap-structurally-valid.md: expected=0 observed=0 PASS
--relay-root addressing/G1-casefold-lineage: expected=0 observed=0 PASS
--relay-root merge/M1-merge-claim-no-auth: expected=1 observed=1 PASS
--relay-root merge/M2-merge-claim-with-auth: expected=0 observed=0 PASS
--relay-root merge/M3-honest-not-merged: expected=0 observed=0 PASS
--relay-root merge/M4-canonical-claim-no-auth: expected=1 observed=1 PASS
--relay-root merge/M5-self-auth-forgery: expected=1 observed=1 PASS
--relay-root merge/M7-continuation-prose-claim-no-auth: expected=1 observed=1 PASS
--relay-root merge/M8-continuation-canonical-claim-no-auth: expected=1 observed=1 PASS
--relay-root merge/BP5-blank-line-merge-evasion: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root merge/GA-flushleft-canonical-no-auth: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root merge/GB-tab-ambiguous-canonical: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root merge/GC-2blank-ambiguous-canonical: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root merge/GD-quoted-canonical-inert: expected=0 observed=0 PASS
--relay-root merge/organic-m2-unauthorized-merge: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root merge-token/MT1-valid-token-grant: expected=0 observed=0 PASS
--relay-root merge-token/MT2-backticked-token-inert: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
merge-token/MT3-self-granted-token.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
merge-token/MT4-multi-to-grant.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
merge-token/MT5-wrong-phase-token.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root merge-token/MT6-token-outside-root: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root merge-token/MT7-duplicate-authorization-decoy: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root merge-token/MT8-denied-only-no-auth: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root merge-token/MT9-cross-dispatch-runroot: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E1-empty-final-git-status.md: expected=1 observed=1 PASS
content/E2-empty-actions-git-ref.md: expected=1 observed=1 PASS
content/E3-empty-scopediff-live-dispatch.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
content/E4-structured-unavailable.md: expected=0 observed=0 PASS
content/E5-clean-tree.md: expected=0 observed=0 PASS
content/E6-out-row-allin.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E7-valid-scopediff-dispatch.md: expected=0 observed=0 PASS
content/E8-structured-none-scopediff.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E9-unparseable-row.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E10-bare-unavailable.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E11-placeholder-field.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E12-colon-rows-valid.md: expected=0 observed=0 PASS
content/E13-na-marker.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E13-scopediff-detached-row.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E14-scopediff-row-after-result.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
content/E15-dual-scopediff-decoy.md: expected=1 observed=1 PASS
  expected_errors=3 observed_errors=3
content/E16-dual-scopediff-contiguous.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
--relay-root rootindex/R-IDX1-valid: expected=0 observed=0 PASS
--relay-root rootindex/R-IDX2-decreasing: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD1-fold-edit-no-foldscope.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
fold/FD2-valid-fold-report.md: expected=0 observed=0 PASS
fold/FD3-out-row-with-edit.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD4-empty-foldscope.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD5-deviation-relay-out-rows-no-edit.md: expected=0 observed=0 PASS
fold/FD6-edit-then-waiver-shape.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD7-fold-no-deviation-uncommitted.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD8-scope-after-actions.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD9-fold-no-wordlist-verbs.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
fold/FD10-detached-row-foldscope.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD11-row-after-result-foldscope.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
fold/FD12-absence-prefixed-actions.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
fold/FD13-dual-foldscope-contiguous.md: expected=1 observed=1 PASS
  expected_errors=2 observed_errors=2
identity/S4a-proxy-from.md: expected=1 observed=1 PASS
--relay-root lineage/LI1-valid-parent-chain: expected=0 observed=0 PASS
--relay-root lineage/LI2-cc-orchestrator-plan-trap: expected=1 observed=1 PASS
--relay-root lineage/LI3-no-plan-review-parent: expected=1 observed=1 PASS
--relay-root lineage/LI4-non-addressee-impl-report: expected=1 observed=1 PASS
--relay-root lineage/LI5-edgeless-delegated-cc-trap: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root lineage/LI6-edgeless-no-approve: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root lineage/LI7-edgeless-non-addressee-report: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root ambiguity/AMB1a-shared-id-dispatch: expected=0 observed=0 PASS
--relay-root ambiguity/AMB1b-shared-id-planhop: expected=0 observed=0 PASS
--relay-root ambiguity/AMB1c-shared-id-implreport: expected=0 observed=0 PASS
--relay-root ambiguity/AMB2-none-qualify: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root ambiguity/AMB3-latest-wins: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
rowtruth/RT1-valid-fold-evidence.md: expected=0 observed=0 PASS
rowtruth/RT2-missing-fold-evidence.md: expected=1 observed=1 PASS
rowtruth/RT3-valid-scope-evidence.md: expected=0 observed=0 PASS
rowtruth/RT4-missing-scope-evidence.md: expected=1 observed=1 PASS
--relay-root rowtruth/RT5-two-relay-out-in-flip: expected=1 observed=1 PASS
--relay-root p9/P9-blank-line-prose-after-block: expected=0 observed=0 PASS
p9/P9b-claim-after-scan-blank-line.md: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root design-review/DR1-valid-design-doc-chain: expected=0 observed=0 PASS
--relay-root design-review/DR2-edge-less-no-review: expected=1 observed=1 PASS
--relay-root design-review/DR3-direct-override: expected=0 observed=0 PASS
--relay-root design-review/DR4-self-override: expected=1 observed=1 PASS
--relay-root design-review/DR5-tiny-no-lock: expected=0 observed=0 PASS
--relay-root design-review/DR6-genuine-audit-record: expected=0 observed=0 PASS
--relay-root design-review/DR7a-audit-record-no-design-doc: expected=0 observed=0 PASS
--relay-root design-review/DR7b-audit-record-design-doc-visible: expected=1 observed=1 PASS
--relay-root design-review/DR8-review-not-from-implementer: expected=1 observed=1 PASS
--relay-root design-review/DR9-verdict-not-approve: expected=1 observed=1 PASS
--relay-root design-review/DR10-review-parent-not-design: expected=1 observed=1 PASS
--relay-root design-review/DR11a-stale-review-parent: expected=1 observed=1 PASS
--relay-root design-review/DR11b-locks-must-revise-v1: expected=1 observed=1 PASS
--relay-root design-review/DR11c-positive-control-v2: expected=0 observed=0 PASS
--relay-root design-review/DR13-omit-record-kind: expected=1 observed=1 PASS
--relay-root design-review/DR14-legacy-lock-tolerated: expected=0 observed=0 PASS
--relay-root design-review/DR15-verdict-human-decision: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root design-review/DR16-F-a-same-id-orch-design: expected=0 observed=0 PASS
--relay-root design-review/DR17-F-relock-latest-before-review: expected=0 observed=0 PASS
--relay-root design-review/DR18-F-thread-one-id-pass: expected=0 observed=0 PASS
--relay-root design-review/DR19-F-thread-mustrevise: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root design-review/DR20-F-b-no-design-parent: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root design-review/DR21-F-b158-no-owner-review: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root design-review/DR22-F-c-cross-owner-design: expected=1 observed=1 PASS
  expected_errors=1 observed_errors=1
--relay-root orch-review/OR1-audit-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR2-design-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR3-plan-delegated-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR4-override-impl-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR5-merge-gate-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR6-review-fold-cc: expected=0 observed=0 PASS
--relay-root orch-review/OR7-audit-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/OR8-design-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/OR9-plan-delegated-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/OR10-override-impl-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/OR11-merge-gate-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/OR12-review-fold-no-reviewer: expected=1 observed=1 PASS
--relay-root orch-review/A1-no-reviewer-no-waiver: expected=1 observed=1 PASS
--relay-root orch-review/A2-operator-waiver: expected=0 observed=0 PASS
--relay-root orch-review/A3-self-waiver: expected=1 observed=1 PASS
--relay-root orch-review/A4-solo-no-orchestrator-relay: expected=0 observed=0 PASS
--relay-root orch-review/EX1-operator-authority: expected=0 observed=0 PASS
--relay-root orch-review/EX2-pairplanner-impl: expected=0 observed=0 PASS
--relay-root orch-review/EX3-reviewer-boot: expected=0 observed=0 PASS
--relay-root orch-review/EX4-reconcile: expected=0 observed=0 PASS
--relay-root orch-review/EX5-proceed-nodeleg: expected=0 observed=0 PASS
--relay-root orch-review/EX6-boot-to-reviewer: expected=0 observed=0 PASS
```

### Timestamp-drift fixture gate

```sh
python3 tools/check-timestamp-drift.py
```

Exit status: 0.

```text
fresh stamp passes                                            PASS
future drift rejected                                         PASS
past drift rejected                                           PASS
historical relay passes without freshness                     PASS
tolerance is tunable and defaults tight                       PASS
impossible time rejected without freshness                    PASS
missing stamp: rejected when authoring, ignored otherwise     PASS
index: good row passes                                        PASS
index: quiet index with an old newest row passes              PASS
index: decreasing row rejected                                PASS
index: row disagreeing with its filename rejected             PASS
index: fabricated future row rejected                         PASS
index: impossible time rejected                               PASS
index: pre-marker history grandfathered                       PASS
index: audit surfaces grandfathered history                   PASS
index: post-marker row below the boundary rejected            PASS
index: post-marker extra raw pipe rejected by header arity    PASS
index: odd backslash escapes a literal status pipe            PASS
index: even backslashes leave a delimiter and reject the row  PASS
index: pre-marker malformed row stays grandfathered           PASS
index: audit reports pre-marker header-arity drift            PASS
index: headerless rows warn without arity errors              PASS
index: matching eight-column header passes                    PASS

23/23 passed
```

### Root AGENTS.md census

```sh
find . -name AGENTS.md -not -path './.git/*'
```

Denominator: all repository paths except `.git/*`; exactly one hit was required.

Exit status: 0.

```text
./AGENTS.md
```

### Ladder byte identity

```sh
tmpd=$(mktemp -d)
awk '/^if \[ -f "\$skills_root\/tools\/relay-lint\.py" \]/,/^  run_lint relay-lint$/' \
  tools/adapters/claude-code/relay-lint-posttooluse.sh > "$tmpd/ladder-hook"
awk '/^if \[ -f "\$skills_root\/tools\/relay-lint\.py" \]/,/^  run_lint relay-lint$/' \
  tools/adapters/claude-code/bash-relay-guard.sh > "$tmpd/ladder-guard"
[ -s "$tmpd/ladder-hook" ] || { echo "EMPTY EXTRACTION — awk range did not match"; exit 1; }
cmp "$tmpd/ladder-hook" "$tmpd/ladder-guard"
```

Exit status: 0.

Literal output: empty.

The non-empty extracted ladders were byte-identical because `cmp` was silent.

### Census Part A: production resolution surfaces

```sh
grep -rn 'codex/skills' tools/adapters/claude-code/relay-lint-posttooluse.sh \
  tools/adapters/claude-code/bash-relay-guard.sh tools/adapters/README.md
```

Denominator: precisely the three named production-resolution/documentation surfaces: `relay-lint-posttooluse.sh`, `bash-relay-guard.sh`, and `tools/adapters/README.md`.

Exit status: 0.

```text
tools/adapters/claude-code/relay-lint-posttooluse.sh:27:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/relay-lint-posttooluse.sh:29:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
tools/adapters/claude-code/bash-relay-guard.sh:46:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/bash-relay-guard.sh:48:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
tools/adapters/README.md:15:`~/.codex/skills` remains a working deprecated fallback destination; new installs use `~/.agents/skills`, matching Codex's own back-compat posture.
tools/adapters/README.md:17:`<skills-root>` means the skills root the installed skill tree was resolved from — each generated plugin tree ships its own `tools/`. Both hooks resolve relay-lint in this order: `$RELAY_LINT_SKILLS_ROOT/tools/relay-lint.py`, `$HOME/.agents/skills/tools/relay-lint.py`, `$HOME/.codex/skills/tools/relay-lint.py` (deprecated), then `relay-lint` on `PATH`.
```

Classification of every Part A hit (6):

| Hit | Classification |
|---|---|
| `relay-lint-posttooluse.sh:27` | deprecated production fallback branch |
| `relay-lint-posttooluse.sh:29` | deprecated production fallback execution |
| `bash-relay-guard.sh:46` | deprecated production fallback branch |
| `bash-relay-guard.sh:48` | deprecated production fallback execution |
| `README.md:15` | documentation: deprecated fallback note |
| `README.md:17` | documentation: resolution-order sentence |

The plan expected a `codex/skills` hit in the D1 comment, but that comment contains no such string, so the stated Part A comment hit was unmeetable and the census records the reachable six-hit denominator instead.

### Hook fallback ordering

```sh
grep -n 'agents/skills\|codex/skills' \
  tools/adapters/claude-code/relay-lint-posttooluse.sh \
  tools/adapters/claude-code/bash-relay-guard.sh
```

Exit status: 0.

```text
tools/adapters/claude-code/relay-lint-posttooluse.sh:25:elif [ -f "$HOME/.agents/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/relay-lint-posttooluse.sh:26:  run_lint python3 "$HOME/.agents/skills/tools/relay-lint.py"
tools/adapters/claude-code/relay-lint-posttooluse.sh:27:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/relay-lint-posttooluse.sh:29:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
tools/adapters/claude-code/bash-relay-guard.sh:44:elif [ -f "$HOME/.agents/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/bash-relay-guard.sh:45:  run_lint python3 "$HOME/.agents/skills/tools/relay-lint.py"
tools/adapters/claude-code/bash-relay-guard.sh:46:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/bash-relay-guard.sh:48:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
```

The line-numbered output proves that `.agents/skills` precedes the deprecated `.codex/skills` fallback in both hooks.

### Census Part B: full population

```sh
grep -rn 'codex/skills' tools/ --include='*.sh' --include='*.md' | grep -v relay-lint-fixtures
```

Denominator: every `*.sh` and `*.md` path recursively under `tools/`, with lines containing `relay-lint-fixtures` removed by the command's final filter.

Exit status: 0.

```text
tools/adapters/claude-code/bash-relay-guard.sh:46:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/bash-relay-guard.sh:48:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
tools/adapters/claude-code/relay-lint-posttooluse.sh:27:elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
tools/adapters/claude-code/relay-lint-posttooluse.sh:29:  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
tools/adapters/claude-code/test-adapter.sh:216:# configured root -> ~/.agents/skills -> deprecated ~/.codex/skills -> PATH.
tools/adapters/claude-code/test-adapter.sh:223:mkdir -p "$fallback_home/.agents/skills/tools" "$fallback_home/.codex/skills/tools"
tools/adapters/claude-code/test-adapter.sh:226:  > "$fallback_home/.codex/skills/tools/relay-lint.py"
tools/adapters/claude-code/test-adapter.sh:274:mkdir -p "$pathbeat_home/.codex/skills/tools"
tools/adapters/claude-code/test-adapter.sh:275:cp "$TOOLS_DIR/relay-lint.py" "$pathbeat_home/.codex/skills/tools/relay-lint.py"
tools/adapters/README.md:15:`~/.codex/skills` remains a working deprecated fallback destination; new installs use `~/.agents/skills`, matching Codex's own back-compat posture.
tools/adapters/README.md:17:`<skills-root>` means the skills root the installed skill tree was resolved from — each generated plugin tree ships its own `tools/`. Both hooks resolve relay-lint in this order: `$RELAY_LINT_SKILLS_ROOT/tools/relay-lint.py`, `$HOME/.agents/skills/tools/relay-lint.py`, `$HOME/.codex/skills/tools/relay-lint.py` (deprecated), then `relay-lint` on `PATH`.
```

Classification of every Part B hit (11):

| Hit | Classification |
|---|---|
| `bash-relay-guard.sh:46` | deprecated production fallback branch |
| `bash-relay-guard.sh:48` | deprecated production fallback execution |
| `relay-lint-posttooluse.sh:27` | deprecated production fallback branch |
| `relay-lint-posttooluse.sh:29` | deprecated production fallback execution |
| `test-adapter.sh:216` | intentional test-fixture: f0–f4 fallback-order test comment |
| `test-adapter.sh:223` | intentional test-fixture: f0–f4 setup creates a deprecated-root test tree |
| `test-adapter.sh:226` | intentional test-fixture: D3 decoy linter written at deprecated root |
| `test-adapter.sh:274` | intentional test-fixture: f3 deprecated-over-PATH retention-case setup |
| `test-adapter.sh:275` | intentional test-fixture: f3 retention case installs the deprecated-root linter |
| `README.md:15` | documentation: deprecated fallback note |
| `README.md:17` | documentation: resolution-order sentence |

Counts: Part A = 6 hits (4 deprecated production fallback, 2 documentation); Part B = 11 hits (4 deprecated production fallback, 2 documentation, 5 intentional test-fixture).

No adapter/generator mismatch was observed and no local source file was edited.
