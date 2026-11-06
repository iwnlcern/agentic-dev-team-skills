ROLE: Orchestrator Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: boot
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: yes
FROM: run.orchestrator-planner
TO: run.orchestrator-reviewer
CC: operator
SUBJECT: fixture

Boot the orchestrator reviewer; no authority dispatch.

FINAL_GIT_STATUS_SHORT: none — fixture no disk changes
