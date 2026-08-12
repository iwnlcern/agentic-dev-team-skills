#!/usr/bin/env python3
"""Generate the initial deterministic mastertier fixture member."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tools" / "relay-lint-fixtures" / "mastertier" / "MT0-generator-smoke.md"
CONTENT = """ROLE: Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: mt0-generator-smoke
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: qi.planner
TO: qi.implementer

Generated fixture smoke control.

ACTIONS_GIT_REF: none — fixture relay only
FINAL_GIT_STATUS_SHORT: none — fixture, clean tree
"""


def main() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(CONTENT, encoding="utf-8")


if __name__ == "__main__":
    main()
