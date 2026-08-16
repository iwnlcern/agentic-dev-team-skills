ROLE: Orchestrator Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: boot-reviewer
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: yes
FROM: run.orchestrator-planner
TO: run.orchestrator-reviewer
CC: operator
SUBJECT: fixture

Load orchestrator-reviewer skill and stand by.

FINAL_GIT_STATUS_SHORT: none — fixture no disk changes
