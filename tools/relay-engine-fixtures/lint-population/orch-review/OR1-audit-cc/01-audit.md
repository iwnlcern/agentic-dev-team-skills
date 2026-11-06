ROLE: Orchestrator Planner
PHASE: AUDIT
AUTHORITY: read-only
DISPATCH_ID: or-audit
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: yes
FROM: run.orchestrator-planner
TO: team-a.planner, team-a.implementer
CC: run.orchestrator-reviewer, operator
SUBJECT: fixture

Body.

FINAL_GIT_STATUS_SHORT: none — fixture no disk changes
