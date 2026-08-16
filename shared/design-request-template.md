<!-- Planner to Implementer DESIGN-REVIEW request (Template C is the response). -->
## Template I — DESIGN request from pair Planner to pair Implementer

Use this after a pair Planner has produced a real design doc and needs the pair Implementer to perform the required read-only DESIGN-REVIEW. This is the Planner request. Template C is the Implementer response.

```text
## Team <id> — <bundle>: DESIGN-REVIEW REQUEST

ROLE: Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: <design request id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes/no + for what>
FROM: <team>.planner
TO: <team>.implementer
CC: <orchestrator | operator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <DESIGN relay dispatch id from orchestrator/operator>
DESIGN_DOC_ID: <logical design id>
DESIGN_RECORD_KIND: design-doc
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>

Current scope for the `TO` addressee: read-only DESIGN-REVIEW. No source/test edits, no implementation branches, no commits, no PRs.

Anti-pattern: `TO: orchestrator` with `<team>.implementer` only on `CC` is not a DESIGN-REVIEW request. CC is context only; it grants no phase authority and creates no review obligation. If this relay is misaddressed, reissue it with `TO: <team>.implementer`.

Design doc to review:
- DESIGN_DOC_ID: <logical design id>
- Location / relay pointer: <path or attachment>
- Selected option:
- Rejected alternatives:
- Boundary contract:
- Acceptance criteria draft:
- Operator decisions/defaults:
- Open questions:

Requested response:
Use Template C (`PHASE: DESIGN-REVIEW`, `FROM: <team>.implementer`, `TO: <team>.planner`, `PARENT_DISPATCH_ID: <this dispatch id>`, same `DESIGN_DOC_ID`) and return `DESIGN_REVIEW_VERDICT: approve | must-revise | reject-narrow | human-decision-required`.

FINAL_GIT_STATUS_SHORT: <paste output or unavailable — reason>
```
