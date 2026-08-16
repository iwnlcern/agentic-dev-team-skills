ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: dr5-plan
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: yes
FROM: pair1.planner
TO: pair1.implementer
CEREMONY_DOWNGRADE: tiny
ESCALATION_SCAN_RESULT: all-no
ESCALATION_SCAN:
  - authz/tenant/RLS/permissions/secrets: no
  - migration/backfill/destructive-write/canonical-data-repair: no
  - money/inventory/orders/planning/accounting/trust-critical-state: no
  - AI-or-automation-acts-downstream: no
  - worker/scheduler/queue/retry/async-side-effect: no
  - cross-repo/service-contract/generated-schema/shared-API-event: no
  - user-visible-control-with-materializer/downstream-consumer: no
  - test-runtime-role-mismatch: no
  - broad-scope-expansion/ambiguous-product-semantics/residual-risk/live-verify-skip: no
WHY_DOWNGRADE_IS_SAFE: all scan rows are no — fixture tiny path
FINAL_GIT_STATUS_SHORT: unavailable — fixture no git workspace
