ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: t1
CEREMONY_TIER: tiny
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
ESCALATION_SCAN:
- migration/backfill/destructive-write/canonical-data-repair: yes — UPDATE backfill
- money/inventory/orders/planning/accounting/trust-critical-state: yes — PO status
ESCALATION_SCAN_RESULT: trigger-present
WHY_DOWNGRADE_IS_SAFE:
- single file, reversible via transaction log
