#!/usr/bin/env python3
"""Run the relay-lint fixture matrix and compare expected exit codes.

This is a dev/release helper. It imports relay-lint directly to avoid shell
harness noise; it does not call any LLM, network, subprocess, or git command.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINT = ROOT / "tools" / "relay-lint.py"
FIXTURES = ROOT / "tools" / "relay-lint-fixtures"

A5_EXPECTED = [
    ("root", "lockdigest/AH1-float-forward/.relays/v29", 0),
    ("root", "lockdigest/AH2-rollback/.relays/v29", 1),
    ("root", "lockdigest/AH3a-later-malformed/.relays/v29", 1),
    ("root", "lockdigest/AH3b-later-conflicted/.relays/v29", 1),
    ("root", "lockdigest/AH4-two-groups/.relays/v29", 1),
    ("root", "lockdigest/AH5a-samebase-distinct/.relays/v29", 1),
    ("root", "lockdigest/AH5b-samebase-identical/.relays/v29", 0),
    ("root", "lockdigest/AH5c-cogovernor-mismatch/.relays/v29", 1),
    ("root", "lockdigest/AH6a-diffbase-distinct/.relays/v29", 1),
    ("root", "lockdigest/AH6b-diffbase-identical/.relays/v29", 0),
    ("root", "lockdigest/AH7-three-carrier-AAB/.relays/v29", 1),
    ("root", "lockdigest/FB1-longer-closer/.relays/v29", 1),
    ("file", "lockdigest/FB2-indented-nonopener.md", 1),
    ("file", "lockdigest/FB3-pseudo-closer-suffix.md", 0),
    ("file", "lockdigest/FB4-unclosed-eof.md", 0),
    ("root", "lockdigest/FB5-three-space-composite/.relays/v29", 1),
    ("file", "lockdigest/FB6a-info-opener.md", 0),
    ("file", "lockdigest/FB6b-backtick-info-nonopener.md", 1),
    ("file", "lockdigest/FB7-nbsp-pseudo-closer.md", 0),
    ("file", "lockdigest/FB8-u2028-pseudo-closer.md", 0),
    ("file", "lockdigest/FLD01-valid-shape.md", 0),
    ("file", "lockdigest/FLD04-digest-no-locator.md", 1),
    ("file", "lockdigest/FLD05-digest-short.md", 1),
    ("file", "lockdigest/FLD06-digest-nonhex.md", 1),
    ("file", "lockdigest/FLD07-digest-upper.md", 1),
    ("file", "lockdigest/FLD08-stem-pathsep.md", 1),
    ("file", "lockdigest/FLD09-stem-atsign.md", 1),
    ("file", "lockdigest/FLD10-stem-mdsuffix.md", 1),
    ("file", "lockdigest/FLD11-badstem-no-digest.md", 0),
    ("file", "lockdigest/FLD12-locator-conflict-no-digest.md", 1),
    ("file", "lockdigest/FLD13-identical-locator-repeat.md", 0),
    ("file", "lockdigest/FLD14-identical-digest-repeat.md", 0),
    ("file", "lockdigest/FLD15-digest-conflict.md", 1),
    ("file", "lockdigest/FLD16-locator-conflict.md", 1),
    ("file", "lockdigest/FLD17-locconflict-plus-baddigest.md", 1),
    ("file", "lockdigest/FLD18-missing-locator-baddigest.md", 1),
    ("file", "lockdigest/FLD19-both-conflict.md", 1),
    ("file", "lockdigest/FLD20-badlocator-digestconflict.md", 1),
    ("file", "lockdigest/FLD21-digestconflict-all-malformed.md", 1),
    ("file", "lockdigest/FLD22-locconflict-all-malformed.md", 1),
    ("file", "lockdigest/FLD23-missing-locator-digestconflict.md", 1),
    ("file", "lockdigest/FLD24-u2028-field-decoy.md", 0),
    ("file", "lockdigest/LDM1-missing/01-plan-20260801-130000.md", 0),
    ("root", "lockdigest/LDM1-missing", 1),
    ("file", "lockdigest/LDM2-mismatch/.relays/v29/01-plan-20260801-130000.md", 0),
    ("root", "lockdigest/LDM2-mismatch/.relays/v29", 1),
    ("file", "lockdigest/MFa-tilde-holds-backtick.md", 0),
    ("file", "lockdigest/MFb-backtick-holds-tilde.md", 0),
    ("root", "lockdigest/RLD1-valid-match/.relays/v29", 0),
    ("root", "lockdigest/RLD2-production-topology/.relays/v29", 0),
    ("root", "lockdigest/RLD5-directory-candidate", 1),
    ("root", "lockdigest/RLD6-every-instance-match/.relays/v29", 0),
    ("root", "lockdigest/RLD7-later-mismatch/.relays/v29", 1),
    ("root", "lockdigest/RLD8-mixed-partition/.relays/v29", 1),
    ("root", "lockdigest/RLD9-read-seam/.relays/v29", 1),
    ("root", "lockdigest/STAMP-impossible-later/.relays/v29", 1),
    ("root", "lockdigest/STAMP2-nonhyphen-governs/.relays/v29", 0),
    ("file", "lockdigest/TF1-tilde-file-shape.md", 0),
    ("root", "lockdigest/TG1-tilde-governance/.relays/v29", 0),
    ("template", "lockdigest/TM1-placeholder.md", 0),
    ("template", "lockdigest/TM2-ordinary-malformed.md", 0),
    ("template", "lockdigest/TM3-conflicting-duplicates.md", 0),
    ("root", "lockdigest/UNSTAMPED-ineligible-control/.relays/v29", 0),
    ("index", "rootres/XR1-marked-resolving", 0),
    ("index", "rootres/XR10-no-relay-cell", 0),
    ("index-rel", "rootres/XR11-near-miss-cell", 1),
    ("index", "rootres/XR12-spaced-root", 0),
    ("index-rel", "rootres/XR13-norows-duplicate-markers", 1),
    ("index-rel", "rootres/XR14-norows-bad-root", 1),
    ("index", "rootres/XR16-u2028-marker-decoy", 0),
    ("index-rel", "rootres/XR2-marked-missing-file", 1),
    ("index", "rootres/XR3-unmarked-unresolvable", 0),
    ("index", "rootres/XR4-duplicate-markers", 1),
    ("index", "rootres/XR5-marker-nonexistent-dir", 1),
    ("index", "rootres/XR6-marker-existing-file", 1),
    ("index-rel", "rootres/XR7-cell-directory", 1),
    ("index", "rootres/XR8-decoys-one-live", 0),
    ("index", "rootres/XR9-deployment/idxhome", 0),
]
A5_ERRORS = {
    "lockdigest/AH1-float-forward/.relays/v29": [],
    "lockdigest/AH2-rollback/.relays/v29": ["02-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7 does not match the declared 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111"],
    "lockdigest/AH3a-later-malformed/.relays/v29": ["01-plan-20260801-100000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7", "02-plan-20260801-130000.md: PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/AH3b-later-conflicted/.relays/v29": ["01-plan-20260801-100000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7", "02-plan-20260801-130000.md: PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/AH4-two-groups/.relays/v29": ["03-plan-20260801-110000.md: PLAN_SHA256: ../plans/other-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7"],
    "lockdigest/AH5a-samebase-distinct/.relays/v29": ["x/01-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses", "y/01-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses"],
    "lockdigest/AH5b-samebase-identical/.relays/v29": [],
    "lockdigest/AH5c-cogovernor-mismatch/.relays/v29": ["x/01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7 does not match the declared 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111", "y/01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7 does not match the declared 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111"],
    "lockdigest/AH6a-diffbase-distinct/.relays/v29": ["01-design-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses", "02-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses"],
    "lockdigest/AH6b-diffbase-identical/.relays/v29": [],
    "lockdigest/AH7-three-carrier-AAB/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses", "02-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses", "03-plan-20260801-130000.md: PLAN_SHA256: stem 'fixture-plan' carries 2 indistinguishable latest declarations; verification refuses"],
    "lockdigest/FB1-longer-closer/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7 does not match the declared 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111"],
    "lockdigest/FB2-indented-nonopener.md": ["PLAN_ARTIFACT value 'plans/fixture-plan' is not a bare filename stem", "PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FB3-pseudo-closer-suffix.md": [],
    "lockdigest/FB4-unclosed-eof.md": [],
    "lockdigest/FB5-three-space-composite/.relays/v29": ["02-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7 does not match the declared 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111"],
    "lockdigest/FB6a-info-opener.md": [],
    "lockdigest/FB6b-backtick-info-nonopener.md": ["PLAN_ARTIFACT value 'plans/fixture-plan' is not a bare filename stem", "PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FB7-nbsp-pseudo-closer.md": [],
    "lockdigest/FB8-u2028-pseudo-closer.md": [],
    "lockdigest/FLD01-valid-shape.md": [],
    "lockdigest/FLD04-digest-no-locator.md": ["PLAN_SHA256 declared with no PLAN_ARTIFACT to locate the artifact"],
    "lockdigest/FLD05-digest-short.md": ["PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FLD06-digest-nonhex.md": ["PLAN_SHA256 value 'gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FLD07-digest-upper.md": ["PLAN_SHA256 value 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FLD08-stem-pathsep.md": ["PLAN_ARTIFACT value 'plans/fixture-plan' is not a bare filename stem"],
    "lockdigest/FLD09-stem-atsign.md": ["PLAN_ARTIFACT value 'fixture-plan @ sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a bare filename stem"],
    "lockdigest/FLD10-stem-mdsuffix.md": ["PLAN_ARTIFACT value 'fixture-plan.md' is not a bare filename stem"],
    "lockdigest/FLD11-badstem-no-digest.md": [],
    "lockdigest/FLD12-locator-conflict-no-digest.md": ["PLAN_ARTIFACT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD13-identical-locator-repeat.md": [],
    "lockdigest/FLD14-identical-digest-repeat.md": [],
    "lockdigest/FLD15-digest-conflict.md": ["PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD16-locator-conflict.md": ["PLAN_ARTIFACT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD17-locconflict-plus-baddigest.md": ["PLAN_ARTIFACT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present", "PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FLD18-missing-locator-baddigest.md": ["PLAN_SHA256 declared with no PLAN_ARTIFACT to locate the artifact", "PLAN_SHA256 value 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a 64-hex lowercase sha256 digest"],
    "lockdigest/FLD19-both-conflict.md": ["PLAN_ARTIFACT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present", "PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD20-badlocator-digestconflict.md": ["PLAN_ARTIFACT value 'plans/fixture-plan' is not a bare filename stem", "PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD21-digestconflict-all-malformed.md": ["PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD22-locconflict-all-malformed.md": ["PLAN_ARTIFACT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"],
    "lockdigest/FLD23-missing-locator-digestconflict.md": ["PLAN_SHA256 carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present", "PLAN_SHA256 declared with no PLAN_ARTIFACT to locate the artifact"],
    "lockdigest/FLD24-u2028-field-decoy.md": [],
    "lockdigest/LDM1-missing/01-plan-20260801-130000.md": [],
    "lockdigest/LDM1-missing": ["01-plan-20260801-130000.md: PLAN_SHA256: no artifact resolves for stem 'no-such-stem' under plans/ at any probe root"],
    "lockdigest/LDM2-mismatch/.relays/v29/01-plan-20260801-130000.md": [],
    "lockdigest/LDM2-mismatch/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7"],
    "lockdigest/MFa-tilde-holds-backtick.md": [],
    "lockdigest/MFb-backtick-holds-tilde.md": [],
    "lockdigest/RLD1-valid-match/.relays/v29": [],
    "lockdigest/RLD2-production-topology/.relays/v29": [],
    "lockdigest/RLD5-directory-candidate": ["01-plan-20260801-130000.md: PLAN_SHA256: plans/fixture-plan.md is not a readable regular file"],
    "lockdigest/RLD6-every-instance-match/.relays/v29": [],
    "lockdigest/RLD7-later-mismatch/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest d27decce1299fb1f1409f6c4a521f905406d036dadb6b5c89ea30e1efcdec539 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7", "01-plan-20260801-130000.md: PLAN_SHA256: ./plans/fixture-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7"],
    "lockdigest/RLD8-mixed-partition/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md digest 996f094b9288d88a28922d21057a66a4d050326cc05148ae393c2d92add5f111 does not match the declared 12e0a66d054eefc9b230fb78f18e7238796f0e70340290a2e580bb42404958f7", "01-plan-20260801-130000.md: PLAN_SHA256: ./plans/fixture-plan.md is not a readable regular file", "01-plan-20260801-130000.md: PLAN_SHA256: plans/fixture-plan.md is not a readable regular file"],
    "lockdigest/RLD9-read-seam/.relays/v29": ["01-plan-20260801-130000.md: PLAN_SHA256: ../plans/fixture-plan.md is not a readable regular file"],
    "lockdigest/STAMP-impossible-later/.relays/v29": ["02-plan-20260231-120000.md: filename timestamp 20260231-120000 is not a real date/time"],
    "lockdigest/STAMP2-nonhyphen-governs/.relays/v29": [],
    "lockdigest/TF1-tilde-file-shape.md": [],
    "lockdigest/TG1-tilde-governance/.relays/v29": [],
    "lockdigest/TM1-placeholder.md": [],
    "lockdigest/TM2-ordinary-malformed.md": [],
    "lockdigest/TM3-conflicting-duplicates.md": [],
    "lockdigest/UNSTAMPED-ineligible-control/.relays/v29": [],
    "rootres/XR1-marked-resolving": [],
    "rootres/XR10-no-relay-cell": [],
    "rootres/XR11-near-miss-cell": ["line 3: file cell '(no relay cut — D-1.4; acks)' does not resolve under the declared root ."],
    "rootres/XR12-spaced-root": [],
    "rootres/XR13-norows-duplicate-markers": ["INDEX declares 2 root markers; at most one is permitted"],
    "rootres/XR14-norows-bad-root": ["root marker value 'no-such-dir' does not resolve to a directory"],
    "rootres/XR16-u2028-marker-decoy": [],
    "rootres/XR2-marked-missing-file": ["line 3: file cell 'AUDIT-planner-20260801-130000.md' does not resolve under the declared root ."],
    "rootres/XR3-unmarked-unresolvable": [],
    "rootres/XR4-duplicate-markers": ["INDEX declares 2 root markers; at most one is permitted"],
    "rootres/XR5-marker-nonexistent-dir": ["root marker value 'no-such-dir' does not resolve to a directory"],
    "rootres/XR6-marker-existing-file": ["root marker value 'AUDIT-planner-20260801-130000.md' does not resolve to a directory"],
    "rootres/XR7-cell-directory": ["line 3: file cell 'somedir' does not resolve under the declared root ."],
    "rootres/XR8-decoys-one-live": [],
    "rootres/XR9-deployment/idxhome": [],
}
A5_CWD = {
    "hardening/XRF2-header-only": "hardening/XRF2-header-only",
    "hardening/XRF3-marker-only": "hardening/XRF3-marker-only",
    "lockdigest/AH1-float-forward/.relays/v29": "lockdigest/AH1-float-forward/cwd",
    "lockdigest/AH2-rollback/.relays/v29": "lockdigest/AH2-rollback/cwd",
    "lockdigest/AH3a-later-malformed/.relays/v29": "lockdigest/AH3a-later-malformed/cwd",
    "lockdigest/AH3b-later-conflicted/.relays/v29": "lockdigest/AH3b-later-conflicted/cwd",
    "lockdigest/AH4-two-groups/.relays/v29": "lockdigest/AH4-two-groups/cwd",
    "lockdigest/AH5c-cogovernor-mismatch/.relays/v29": "lockdigest/AH5c-cogovernor-mismatch/cwd",
    "lockdigest/FB1-longer-closer/.relays/v29": "lockdigest/FB1-longer-closer/cwd",
    "lockdigest/FB5-three-space-composite/.relays/v29": "lockdigest/FB5-three-space-composite/cwd",
    "lockdigest/LDM2-mismatch/.relays/v29": "lockdigest/LDM2-mismatch/cwd",
    "lockdigest/RLD6-every-instance-match/.relays/v29": "lockdigest/RLD6-every-instance-match/cwd",
    "lockdigest/RLD7-later-mismatch/.relays/v29": "lockdigest/RLD7-later-mismatch/cwd",
    "lockdigest/RLD8-mixed-partition/.relays/v29": "lockdigest/RLD8-mixed-partition/cwd",
    "lockdigest/RLD9-read-seam/.relays/v29": "lockdigest/RLD9-read-seam/cwd",
    "lockdigest/STAMP-impossible-later/.relays/v29": "lockdigest/STAMP-impossible-later/cwd",
    "lockdigest/TG1-tilde-governance/.relays/v29": "lockdigest/TG1-tilde-governance/cwd",
    "rootres/XR11-near-miss-cell": "rootres/XR11-near-miss-cell",
    "rootres/XR13-norows-duplicate-markers": "rootres/XR13-norows-duplicate-markers",
    "rootres/XR14-norows-bad-root": "rootres/XR14-norows-bad-root",
    "rootres/XR2-marked-missing-file": "rootres/XR2-marked-missing-file",
    "rootres/XR7-cell-directory": "rootres/XR7-cell-directory",
    "rootres/XR9-deployment/idxhome": "rootres/XR9-deployment/cwd",
}
A5_SEAM = {
    "lockdigest/RLD8-mixed-partition/.relays/v29": "lockdigest/RLD8-mixed-partition/cwd/plans/fixture-plan.md",
    "lockdigest/RLD9-read-seam/.relays/v29": "lockdigest/RLD9-read-seam/plans/fixture-plan.md",
}

EXPECTED = A5_EXPECTED + [
    ("file", "mastertier/MT0-generator-smoke.md", 0),
    ("file", "hardening/HG32a-operator-valid.md", 0),
    ("file", "hardening/HG32b-operator-mismatch.md", 1),
    ("index", "hardening/XRF1-fresh-index", 0),
    ("index-rel", "hardening/XRF2-header-only", 1),
    ("index-rel", "hardening/XRF3-marker-only", 1),
    ("index", "hardening/XRF4-root-display", 1),
    ("file", "hardening/FTE1-prose-not-delegated.md", 0),
    ("file", "hardening/FTE2-structural-no-scopediff.md", 1),
    ("file", "hardening/CD1-downgrade-none.md", 0),
    ("file", "hardening/CD2-downgrade-real.md", 1),
    ("file", "hardening/DTRc3-detached-after-blank.md", 1),
    ("file", "hardening/DTRc4-detached-after-result.md", 1),
    ("file", "hardening/FTAD1-filled-dispatch-conformant.md", 0),
    ("root", "kr8a/KR8A1-carrier-exempt", 0),
    ("root", "kr8a/KR8A2-nocarrier-control", 1),
    ("root", "kr8a/KR8A3-implementer-token-control", 1),
    ("root", "kr8a/KR8A4-fenced-token-control", 1),
    ("file", "rolevocab/RV1a-pair-planner.md", 0),
    ("file", "rolevocab/RV1b-pair-implementer.md", 0),
    ("file", "rolevocab/RV2a-master-planner.md", 0),
    ("file", "rolevocab/RV2b-master-reviewer.md", 0),
    ("file", "rolevocab/RV3a-domain-planner.md", 0),
    ("file", "rolevocab/RV3b-domain-reviewer.md", 0),
    ("file", "rolevocab/RV4-invalid-role.md", 1),
    ("file", "rolevocab/RV5-planner-from-pair-planner.md", 0),
    ("file", "rolevocab/RV6-pair-implementer-from-implementer.md", 0),
    ("file", "rolevocab/RV7a-master-dispatch-failclosed.md", 1),
    ("file", "rolevocab/RV7b-domain-direct-override.md", 1),
    ("file", "rolevocab/RV7e-domain-designlock-kind.md", 1),
    ("file", "rolevocab/RV7e2-domain-designlock-id-only.md", 1),
    ("file", "rolevocab/RV7f-master-delegated-authority.md", 1),
    ("file", "rolevocab/RV7h-delegated-bypass-order.md", 1),
    ("file", "rolevocab/RV7i-lock-empty-first.md", 1),
    ("file", "rolevocab/RV7j-override-then-conflict.md", 1),
    ("file", "rolevocab/RV7k-samevalue-duplicate.md", 1),
    ("file", "rolevocab/RV7l-override-plus-lock-conflict.md", 1),
    ("file", "rolevocab/RV7m-kind-conflict-plus-lock.md", 1),
    ("file", "rolevocab/RV7n-delegated-replacement.md", 1),
    ("file", "rolevocab/RV7o-lock-replacement.md", 1),
    ("root", "rolevocab/RV5-new-vocab-chain", 0),
    ("root", "rolevocab/RV5d-newvocab-designdoc-chain", 0),
    ("root", "rolevocab/RV5dneg-designdoc-no-approve", 1),
    ("root", "rolevocab/RV6-mixed-vocab-chain", 0),
    ("root", "rolevocab/RV6i-mixed-dispatch-implreport", 0),
    ("root", "rolevocab/RV7a-master-dispatch-failclosed", 1),
    ("root", "rolevocab/RV7b-domain-direct-override", 1),
    ("root", "rolevocab/RV7e-domain-designlock-kind", 1),
    ("root", "rolevocab/RV7e2-domain-designlock-id-only", 1),
    ("root", "rolevocab/RV7f-master-delegated-authority", 1),
    ("root", "rolevocab/RV7g-malformed-address-hardening", 1),
    ("root", "rolevocab/RV7h-delegated-bypass-order", 1),
    ("root", "rolevocab/RV7i-lock-empty-first", 1),
    ("root", "rolevocab/RV7j-override-then-conflict", 1),
    ("root", "rolevocab/RV7k-samevalue-duplicate", 1),
    ("root", "rolevocab/RV7l-override-plus-lock-conflict", 1),
    ("root", "rolevocab/RV7m-kind-conflict-plus-lock", 1),
    ("root", "rolevocab/RV7n-delegated-replacement", 1),
    ("root", "rolevocab/RV7o-lock-replacement", 1),
    ("root", "rolevocab/AMB4-multiholder-malformed", 1),
    ("root", "rolevocab/RV7c-master-merge-grant", 1),
    ("root", "rolevocab/RV7d-master-broadset-no-fire", 0),
    ("root", "rolevocab/RVn1-cross-owner-review", 1),
    ("root", "rolevocab/RVn2-wrong-to", 1),
    ("root", "rolevocab/RVn3-non-addressee-implreport", 1),
    ("root", "rolevocab/RVn4-cross-role-implreport", 1),
    ("root", "rolevocab/AMB5-singleton-retrospective", 1),
    ("root", "rolevocab/AMB6-alias-retrospective", 1),
    ("root", "rolevocab/AMB7-dispatch-late-review", 1),
    ("root", "rolevocab/AMB8-review-late-plan", 1),
    ("root", "rolevocab/AMB9-late-tokenless", 1),
    ("root", "rolevocab/AMB10-late-wrong-owner", 1),
    ("root", "rolevocab/AMB11-late-malformed-pair", 1),
    ("root", "rolevocab/AMB12-late-cross-role", 1),
    ("root", "rolevocab/AMB13-late-two-from", 1),
    ("root", "rolevocab/AMB14-late-two-to", 1),
    ("root", "rolevocab/KR4A-direct-issuer-prose", 0),
    ("root", "rolevocab/KR4B-planner-prose-nofield", 0),
    ("index", "indexmarker/IDX1-below-floor", 1),
    ("index", "indexmarker/IDX2-first-marker-floor", 1),
    ("index", "indexmarker/IDX3-above-floor", 0),
    ("file", "claude/A1-valid-audit.md", 0),
    ("file", "claude/A2-valid-downgrade.md", 0),
    ("file", "claude/B2-why-before-scan.md", 1),
    ("file", "claude/B3-yes-row-why.md", 1),
    ("file", "claude/B5-audit-no-final.md", 1),
    ("file", "claude/B6-audit-impl-authority.md", 1),
    ("file", "claude/B7-delegated-no-scopediff.md", 1),
    ("file", "claude/B8-deviation-dispatch.md", 1),
    ("file", "claude/B9-bad-enum.md", 1),
    ("file", "claude/C1-evasive-rows.md", 1),
    ("file", "claude/C2-enum-bypass.md", 1),
    ("file", "lint-test/bad1.md", 1),
    ("file", "lint-test/bad2.md", 1),
    ("file", "lint-test/bad3.md", 1),
    ("file", "lint-test/bad3b.md", 1),
    ("file", "lint-test/bad4.md", 1),
    ("file", "lint-test/good5.md", 0),
    ("root", "claude/L1", 0),
    ("root", "claude/L2", 0),
    ("root", "claude/L3", 1),
    ("file", "probes/N2-refusal-code-span.md", 0),
    ("file", "probes/N3-fenced-dispatch-inert.md", 0),
    ("file", "probes/N4-scan-result-mismatch.md", 1),
    ("file", "probes/N5-missing-scan-row.md", 1),
    ("file", "addressing/T1-valid-addressed-dispatch.md", 0),
    ("file", "addressing/T2-token-no-to.md", 1),
    ("file", "addressing/T3-token-to-planner.md", 1),
    ("file", "addressing/T4-token-two-implementers.md", 1),
    ("file", "addressing/T5-bad-address.md", 1),
    ("file", "addressing/T6-valid-audit-both-to.md", 0),
    ("file", "addressing/T7-valid-cc-context.md", 0),
    ("file", "addressing/T8-cc-trap-structurally-valid.md", 0),
    ("root", "addressing/G1-casefold-lineage", 0),
    ("root", "merge/M1-merge-claim-no-auth", 1),
    ("root", "merge/M2-merge-claim-with-auth", 0),
    ("root", "merge/M3-honest-not-merged", 0),
    ("root", "merge/M4-canonical-claim-no-auth", 1),
    ("root", "merge/M5-self-auth-forgery", 1),
    ("root", "merge/M7-continuation-prose-claim-no-auth", 1),
    ("root", "merge/M8-continuation-canonical-claim-no-auth", 1),
    ("root", "merge/BP5-blank-line-merge-evasion", 1),
    ("root", "merge/GA-flushleft-canonical-no-auth", 1),
    ("root", "merge/GB-tab-ambiguous-canonical", 1),
    ("root", "merge/GC-2blank-ambiguous-canonical", 1),
    ("root", "merge/GD-quoted-canonical-inert", 0),
    ("root", "merge/organic-m2-unauthorized-merge", 1),
    ("root", "merge-token/MT1-valid-token-grant", 0),
    ("root", "merge-token/MT2-backticked-token-inert", 1),
    ("file", "merge-token/MT3-self-granted-token.md", 1),
    ("file", "merge-token/MT4-multi-to-grant.md", 1),
    ("file", "merge-token/MT5-wrong-phase-token.md", 1),
    ("root", "merge-token/MT6-token-outside-root", 1),
    ("root", "merge-token/MT7-duplicate-authorization-decoy", 1),
    ("root", "merge-token/MT8-denied-only-no-auth", 1),
    ("root", "merge-token/MT9-cross-dispatch-runroot", 1),
    ("file", "content/E1-empty-final-git-status.md", 1),
    ("file", "content/E2-empty-actions-git-ref.md", 1),
    ("file", "content/E3-empty-scopediff-live-dispatch.md", 1),
    ("file", "content/E4-structured-unavailable.md", 0),
    ("file", "content/E5-clean-tree.md", 0),
    ("file", "content/E6-out-row-allin.md", 1),
    ("file", "content/E7-valid-scopediff-dispatch.md", 0),
    ("file", "content/E8-structured-none-scopediff.md", 1),
    ("file", "content/E9-unparseable-row.md", 1),
    ("file", "content/E10-bare-unavailable.md", 1),
    ("file", "content/E11-placeholder-field.md", 1),
    ("file", "content/E12-colon-rows-valid.md", 0),
    ("file", "content/E13-na-marker.md", 1),
    ("file", "content/E13-scopediff-detached-row.md", 1),
    ("file", "content/E14-scopediff-row-after-result.md", 1),
    ("file", "content/E15-dual-scopediff-decoy.md", 1),
    ("file", "content/E16-dual-scopediff-contiguous.md", 1),
    ("root", "rootindex/R-IDX1-valid", 0),
    ("root", "rootindex/R-IDX2-decreasing", 1),
    ("file", "fold/FD1-fold-edit-no-foldscope.md", 1),
    ("file", "fold/FD2-valid-fold-report.md", 0),
    ("file", "fold/FD3-out-row-with-edit.md", 1),
    ("file", "fold/FD4-empty-foldscope.md", 1),
    ("file", "fold/FD5-deviation-relay-out-rows-no-edit.md", 0),
    ("file", "fold/FD6-edit-then-waiver-shape.md", 1),
    ("file", "fold/FD7-fold-no-deviation-uncommitted.md", 1),
    ("file", "fold/FD8-scope-after-actions.md", 1),
    ("file", "fold/FD9-fold-no-wordlist-verbs.md", 1),
    ("file", "fold/FD10-detached-row-foldscope.md", 1),
    ("file", "fold/FD11-row-after-result-foldscope.md", 1),
    ("file", "fold/FD12-absence-prefixed-actions.md", 1),
    ("file", "fold/FD13-dual-foldscope-contiguous.md", 1),
    ("file", "identity/S4a-proxy-from.md", 1),
    ("root", "lineage/LI1-valid-parent-chain", 0),
    ("root", "lineage/LI2-cc-orchestrator-plan-trap", 1),
    ("root", "lineage/LI3-no-plan-review-parent", 1),
    ("root", "lineage/LI4-non-addressee-impl-report", 1),
    ("root", "lineage/LI5-edgeless-delegated-cc-trap", 1),
    ("root", "lineage/LI6-edgeless-no-approve", 1),
    ("root", "lineage/LI7-edgeless-non-addressee-report", 1),
    ("root", "ambiguity/AMB1a-shared-id-dispatch", 0),
    ("root", "ambiguity/AMB1b-shared-id-planhop", 0),
    ("root", "ambiguity/AMB1c-shared-id-implreport", 0),
    ("root", "ambiguity/AMB2-none-qualify", 1),
    ("root", "ambiguity/AMB3-latest-wins", 1),
    ("file", "rowtruth/RT1-valid-fold-evidence.md", 0),
    ("file", "rowtruth/RT2-missing-fold-evidence.md", 1),
    ("file", "rowtruth/RT3-valid-scope-evidence.md", 0),
    ("file", "rowtruth/RT4-missing-scope-evidence.md", 1),
    ("root", "rowtruth/RT5-two-relay-out-in-flip", 1),
    ("root", "p9/P9-blank-line-prose-after-block", 0),
    ("file", "p9/P9b-claim-after-scan-blank-line.md", 1),
    ("root", "design-review/DR1-valid-design-doc-chain", 0),
    ("root", "design-review/DR2-edge-less-no-review", 1),
    ("root", "design-review/DR3-direct-override", 0),
    ("root", "design-review/DR4-self-override", 1),
    ("root", "design-review/DR5-tiny-no-lock", 0),
    ("root", "design-review/DR6-genuine-audit-record", 0),
    ("root", "design-review/DR7a-audit-record-no-design-doc", 0),
    ("root", "design-review/DR7b-audit-record-design-doc-visible", 1),
    ("root", "design-review/DR8-review-not-from-implementer", 1),
    ("root", "design-review/DR9-verdict-not-approve", 1),
    ("root", "design-review/DR10-review-parent-not-design", 1),
    ("root", "design-review/DR11a-stale-review-parent", 1),
    ("root", "design-review/DR11b-locks-must-revise-v1", 1),
    ("root", "design-review/DR11c-positive-control-v2", 0),
    ("root", "design-review/DR13-omit-record-kind", 1),
    ("root", "design-review/DR14-legacy-lock-tolerated", 0),
    ("root", "design-review/DR15-verdict-human-decision", 1),
    ("root", "design-review/DR16-F-a-same-id-orch-design", 0),
    ("root", "design-review/DR17-F-relock-latest-before-review", 0),
    ("root", "design-review/DR18-F-thread-one-id-pass", 0),
    ("root", "design-review/DR19-F-thread-mustrevise", 1),
    ("root", "design-review/DR20-F-b-no-design-parent", 1),
    ("root", "design-review/DR21-F-b158-no-owner-review", 1),
    ("root", "design-review/DR22-F-c-cross-owner-design", 1),
    ("root", "orch-review/OR1-audit-cc", 0),
    ("root", "orch-review/OR2-design-cc", 0),
    ("root", "orch-review/OR3-plan-delegated-cc", 0),
    ("root", "orch-review/OR4-override-impl-cc", 0),
    ("root", "orch-review/OR5-merge-gate-cc", 0),
    ("root", "orch-review/OR6-review-fold-cc", 0),
    ("root", "orch-review/OR7-audit-no-reviewer", 1),
    ("root", "orch-review/OR8-design-no-reviewer", 1),
    ("root", "orch-review/OR9-plan-delegated-no-reviewer", 1),
    ("root", "orch-review/OR10-override-impl-no-reviewer", 1),
    ("root", "orch-review/OR11-merge-gate-no-reviewer", 1),
    ("root", "orch-review/OR12-review-fold-no-reviewer", 1),
    ("root", "orch-review/A1-no-reviewer-no-waiver", 1),
    ("root", "orch-review/A2-operator-waiver", 0),
    ("root", "orch-review/A3-self-waiver", 1),
    ("root", "orch-review/A4-solo-no-orchestrator-relay", 0),
    ("root", "orch-review/EX1-operator-authority", 0),
    ("root", "orch-review/EX2-pairplanner-impl", 0),
    ("root", "orch-review/EX3-reviewer-boot", 0),
    ("root", "orch-review/EX4-reconcile", 0),
    ("root", "orch-review/EX5-proceed-nodeleg", 0),
    ("root", "orch-review/EX6-boot-to-reviewer", 0),
    ("root", "lockpath/LP1-old-form/.relays/v29", 0),
    ("root", "lockpath/LP2-new-form", 0),
    ("root", "lockpath/LP3-missing", 1),
    ("root", "lockpath/LP4-bare-id", 0),
]

EXPECTED_ERROR_SET = {
    "hardening/HG32a-operator-valid.md": [],
    "hardening/HG32b-operator-mismatch.md": [
        "ROLE/FROM mismatch: ROLE='Operator' but FROM='qi.planner'; do not proxy-author another seat's relay",
    ],
    "hardening/XRF1-fresh-index": [],
    "hardening/XRF2-header-only": [
        "no index rows found in INDEX.md",
    ],
    "hardening/XRF3-marker-only": [
        "no index rows found in INDEX.md",
    ],
    "hardening/XRF4-root-display": [
        "line 3: file cell 'PLAN-planner-20260101-120000.md' does not resolve under the declared root .",
    ],
    "hardening/FTE1-prose-not-delegated.md": [],
    "hardening/FTE2-structural-no-scopediff.md": [
        'delegated DISPATCH IMPL missing SCOPE_DIFF',
        'delegated DISPATCH IMPL missing SCOPE_DIFF_RESULT',
    ],
    "hardening/CD1-downgrade-none.md": [],
    "hardening/CD2-downgrade-real.md": [
        'downgrade/waiver present but ESCALATION_SCAN is missing',
        'downgrade/waiver present but ESCALATION_SCAN_RESULT is missing',
        'missing canonical ESCALATION_SCAN row: AI-or-automation-acts-downstream',
        'missing canonical ESCALATION_SCAN row: authz/tenant/RLS/permissions/secrets',
        'missing canonical ESCALATION_SCAN row: broad-scope-expansion/ambiguous-product-semantics/residual-risk/live-verify-skip',
        'missing canonical ESCALATION_SCAN row: cross-repo/service-contract/generated-schema/shared-API-event',
        'missing canonical ESCALATION_SCAN row: migration/backfill/destructive-write/canonical-data-repair',
        'missing canonical ESCALATION_SCAN row: money/inventory/orders/planning/accounting/trust-critical-state',
        'missing canonical ESCALATION_SCAN row: test-runtime-role-mismatch',
        'missing canonical ESCALATION_SCAN row: user-visible-control-with-materializer/downstream-consumer',
        'missing canonical ESCALATION_SCAN row: worker/scheduler/queue/retry/async-side-effect',
    ],
    "hardening/DTRc3-detached-after-blank.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/extra.ts -> in'; rows must sit contiguously under their header",
    ],
    "hardening/DTRc4-detached-after-result.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/late.ts -> OUT'; rows must sit contiguously under their header",
    ],
    "hardening/FTAD1-filled-dispatch-conformant.md": [],
    "kr8a/KR8A1-carrier-exempt": [],
    "kr8a/KR8A2-nocarrier-control": [
        "02-report.md: IMPL report parent must be a DISPATCH IMPL relay",
    ],
    "kr8a/KR8A3-implementer-token-control": [
        "02-report.md: IMPL report parent must be a DISPATCH IMPL relay",
    ],
    "kr8a/KR8A4-fenced-token-control": [
        "02-report.md: IMPL report parent must be a DISPATCH IMPL relay",
    ],
    "lockpath/LP1-old-form/.relays/v29": [],
    "lockpath/LP2-new-form": [],
    "lockpath/LP3-missing": [
        "01-plan.md: PLAN_LOCK_ID references missing file no-such-file.md",
    ],
    "lockpath/LP4-bare-id": [],
    "rolevocab/RV7a-master-dispatch-failclosed.md": [
        "authority semantics for FROM role 'master-planner' are unruled; a dispatch token from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7b-domain-direct-override.md": [
        "authority semantics for FROM role 'domain-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7e-domain-designlock-kind.md": [
        "authority semantics for FROM role 'domain-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7e2-domain-designlock-id-only.md": [
        "authority semantics for FROM role 'domain-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7f-master-delegated-authority.md": [
        "authority semantics for FROM role 'master-planner' are unruled; a delegated-dispatch-authority claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7h-delegated-bypass-order.md": [
        "DELEGATED_DISPATCH_AUTHORITY carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7i-lock-empty-first.md": [
        "DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7j-override-then-conflict.md": [
        "DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7k-samevalue-duplicate.md": [
        "authority semantics for FROM role 'master-planner' are unruled; a delegated-dispatch-authority claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7l-override-plus-lock-conflict.md": [
        "authority semantics for FROM role 'master-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed; no shipped ruling defines this seat's authority",
        "DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7m-kind-conflict-plus-lock.md": [
        "DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
        "authority semantics for FROM role 'master-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7n-delegated-replacement.md": [
        "DELEGATED_DISPATCH_AUTHORITY carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7o-lock-replacement.md": [
        "DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV5dneg-designdoc-no-approve": [
        "03-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "rolevocab/RV7a-master-dispatch-failclosed": [
        "RV7a-master-dispatch-failclosed.md: authority semantics for FROM role 'master-planner' are unruled; a dispatch token from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7b-domain-direct-override": [
        "RV7b-domain-direct-override.md: authority semantics for FROM role 'domain-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7e-domain-designlock-kind": [
        "01-plan.md: authority semantics for FROM role 'domain-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7e2-domain-designlock-id-only": [
        "01-plan.md: authority semantics for FROM role 'domain-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7f-master-delegated-authority": [
        "01-plan.md: authority semantics for FROM role 'master-planner' are unruled; a delegated-dispatch-authority claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7g-malformed-address-hardening": [
        "01-dispatch.md: TO has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "01-dispatch.md: DISPATCH IMPL requires TO to be exactly one implementer-role address",
        "02-impl-report.md: FROM has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "02-impl-report.md: IMPL report FROM 'qi.grand-vizier' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/RV7h-delegated-bypass-order": [
        "01-plan.md: DELEGATED_DISPATCH_AUTHORITY carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7i-lock-empty-first": [
        "01-plan.md: DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7j-override-then-conflict": [
        "01-plan.md: DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7k-samevalue-duplicate": [
        "01-plan.md: authority semantics for FROM role 'master-planner' are unruled; a delegated-dispatch-authority claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7l-override-plus-lock-conflict": [
        "01-plan.md: authority semantics for FROM role 'master-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed; no shipped ruling defines this seat's authority",
        "01-plan.md: DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7m-kind-conflict-plus-lock": [
        "01-plan.md: DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
        "01-plan.md: authority semantics for FROM role 'master-planner' are unruled; a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority",
    ],
    "rolevocab/RV7n-delegated-replacement": [
        "01-plan.md: DELEGATED_DISPATCH_AUTHORITY carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/RV7o-lock-replacement": [
        "01-plan.md: DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ],
    "rolevocab/AMB4-multiholder-malformed": [
        "02-dispatch-malformed-to.md: TO has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "02-dispatch-malformed-to.md: DISPATCH IMPL requires TO to be exactly one implementer-role address",
        "03-impl-report.md: FROM has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "03-impl-report.md: IMPL report parent 'amb4' is held by 2 relays (01-noise.md, 02-dispatch-malformed-to.md); none is an earlier DISPATCH IMPL relay addressed to qi.grand-vizier",
    ],
    "rolevocab/RV7c-master-merge-grant": [
        "02-impl-report.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "rolevocab/RVn1-cross-owner-review": [
        "03-dispatch.md: PLAN-REVIEW parent must be FROM qi.implementer",
    ],
    "rolevocab/RVn2-wrong-to": [
        "03-dispatch.md: DISPATCH IMPL requires TO to be exactly one implementer-role address",
    ],
    "rolevocab/RVn3-non-addressee-implreport": [
        "02-impl-report.md: IMPL report FROM 'zz.pair-implementer' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/RVn4-cross-role-implreport": [
        "02-impl-report.md: IMPL report FROM 'qi.pair-planner' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/AMB5-singleton-retrospective": [
        "01-impl-report.md: IMPL report parent 'amb5' is held by 1 relays (02-dispatch.md); none is an earlier DISPATCH IMPL relay addressed to qi.implementer",
    ],
    "rolevocab/AMB6-alias-retrospective": [
        "01-impl-report.md: IMPL report parent 'amb6' is held by 1 relays (02-dispatch.md); none is an earlier DISPATCH IMPL relay addressed to qi.pair-implementer",
    ],
    "rolevocab/AMB7-dispatch-late-review": [
        "02-dispatch.md: DISPATCH IMPL parent 'amb7rev' is not earlier than the dispatch relay",
    ],
    "rolevocab/AMB8-review-late-plan": [
        "03-dispatch.md: pair-Planner PLAN parent is not earlier than the PLAN-REVIEW relay",
    ],
    "rolevocab/AMB9-late-tokenless": [
        "01-impl-report.md: IMPL report parent must be a DISPATCH IMPL relay",
    ],
    "rolevocab/AMB10-late-wrong-owner": [
        "01-impl-report.md: IMPL report FROM 'qi.implementer' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/AMB11-late-malformed-pair": [
        "01-impl-report.md: FROM has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "02-dispatch.md: TO has invalid address 'qi.grand-vizier'; expected operator, orchestrator, or <owner>.<role>",
        "02-dispatch.md: DISPATCH IMPL requires TO to be exactly one implementer-role address",
        "01-impl-report.md: IMPL report FROM 'qi.grand-vizier' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/AMB12-late-cross-role": [
        "01-impl-report.md: IMPL report FROM 'qi.pair-planner' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "rolevocab/AMB13-late-two-from": [
        "01-impl-report.md: FROM must contain exactly one address",
    ],
    "rolevocab/AMB14-late-two-to": [
        "02-dispatch.md: DISPATCH IMPL requires exactly one TO addressee",
    ],
    "rolevocab/KR4A-direct-issuer-prose": [],
    "rolevocab/KR4B-planner-prose-nofield": [],
    "indexmarker/IDX1-below-floor": [
        "line 4: index time 20260801-110000 predates the monotonic-from boundary 20260801-120000",
    ],
    "indexmarker/IDX2-first-marker-floor": [
        "line 5: index time 20260801-110000 predates the monotonic-from boundary 20260801-120000",
    ],
    "indexmarker/IDX3-above-floor": [],
    "design-review/DR15-verdict-human-decision": [
        "03-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "design-review/DR19-F-thread-mustrevise": [
        "05-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "design-review/DR20-F-b-no-design-parent": [
        "04-plan.md: DESIGN-REVIEW parent lacks a resolvable DESIGN parent",
    ],
    "design-review/DR21-F-b158-no-owner-review": [
        "03-plan.md: design-doc PLAN parent 'fb158-review' does not resolve to a relay in this lineage",
    ],
    "design-review/DR22-F-c-cross-owner-design": [
        "04-plan.md: DESIGN-REVIEW parent lacks a resolvable DESIGN parent",
    ],
    "merge/BP5-blank-line-merge-evasion": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 13; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GA-flushleft-canonical-no-auth": [
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GB-tab-ambiguous-canonical": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 13; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GC-2blank-ambiguous-canonical": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 14; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/organic-m2-unauthorized-merge": [
        "IMPL-report-20260610-121000.md: missing required header field HUMAN_GATE_REQUIRED",
        "IMPL-report-20260610-121000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT2-backticked-token-inert": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT3-self-granted-token.md": [
        "DISPATCH MERGE FROM must be operator, orchestrator, or an orchestrator-planner-role address",
    ],
    "merge-token/MT4-multi-to-grant.md": [
        "DISPATCH MERGE requires exactly one TO addressee",
    ],
    "merge-token/MT5-wrong-phase-token.md": [
        "DISPATCH MERGE is valid only in PHASE: MERGE-GATE relay files",
    ],
    "merge-token/MT6-token-outside-root": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT7-duplicate-authorization-decoy": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
        "MERGE-GATE-20260610-130000.md: duplicate/conflicting merge authorization; the grant of record carries exactly one verdict line",
    ],
    "merge-token/MT8-denied-only-no-auth": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT9-cross-dispatch-runroot": [
        "d1/IMPL-report-20260611-121000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "content/E3-empty-scopediff-live-dispatch.md": [
        "FINAL_GIT_STATUS_SHORT carries a placeholder, not content; placeholders are valid only in --templates mode",
        "delegated DISPATCH IMPL requires SCOPE_DIFF with >=1 parsed '<path> -> in|OUT' row; a vacuous or structured-none diff does not authorize dispatch",
    ],
    "content/E6-out-row-allin.md": [
        "SCOPE_DIFF_RESULT: all-in inconsistent with an OUT row",
    ],
    "content/E8-structured-none-scopediff.md": [
        "delegated DISPATCH IMPL requires SCOPE_DIFF with >=1 parsed '<path> -> in|OUT' row; a vacuous or structured-none diff does not authorize dispatch",
    ],
    "content/E9-unparseable-row.md": [
        "unparseable SCOPE_DIFF row: 'also touching the auth module a bit'",
    ],
    "content/E10-bare-unavailable.md": [
        "FINAL_GIT_STATUS_SHORT uses a reserved absence word without a reason; use 'unavailable — <reason>' / 'none — <reason>'",
    ],
    "content/E11-placeholder-field.md": [
        "FINAL_GIT_STATUS_SHORT carries a placeholder, not content; placeholders are valid only in --templates mode",
    ],
    "content/E13-na-marker.md": [
        "FINAL_GIT_STATUS_SHORT uses a reserved absence word without a reason; use 'unavailable — <reason>' / 'none — <reason>'",
    ],
    "content/E13-scopediff-detached-row.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E14-scopediff-row-after-result.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E15-dual-scopediff-decoy.md": [
        "duplicate SCOPE_DIFF block; the report of record carries exactly one",
        "duplicate SCOPE_DIFF_RESULT block; the report of record carries exactly one",
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E16-dual-scopediff-contiguous.md": [
        "duplicate SCOPE_DIFF block; the report of record carries exactly one",
        "duplicate SCOPE_DIFF_RESULT block; the report of record carries exactly one",
    ],
    "fold/FD1-fold-edit-no-foldscope.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD3-out-row-with-edit.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD4-empty-foldscope.md": [
        "FOLD_SCOPE requires >=1 parsed '<path> -> in|out' row; an empty scope list does not license a fold edit",
    ],
    "fold/FD6-edit-then-waiver-shape.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD7-fold-no-deviation-uncommitted.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD8-scope-after-actions.md": [
        "FOLD_SCOPE appears after ACTIONS_GIT_REF; the scope artifact precedes the action claim (field-ordering)",
    ],
    "fold/FD9-fold-no-wordlist-verbs.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD10-detached-row-foldscope.md": [
        "row-shaped line outside the FOLD_SCOPE block: '- src/format.js -> out'; rows must sit contiguously under their header",
    ],
    "fold/FD11-row-after-result-foldscope.md": [
        "row-shaped line outside the FOLD_SCOPE block: '- src/format.js -> out'; rows must sit contiguously under their header",
    ],
    "fold/FD12-absence-prefixed-actions.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD13-dual-foldscope-contiguous.md": [
        "duplicate FOLD_SCOPE block; the report of record carries exactly one",
        "duplicate FOLD_SCOPE_RESULT block; the report of record carries exactly one",
    ],
    "lineage/LI5-edgeless-delegated-cc-trap": [
        "03-edgeless-dispatch.md: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch",
    ],
    "lineage/LI6-edgeless-no-approve": [
        "01-edgeless-dispatch.md: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch",
    ],
    "lineage/LI7-edgeless-non-addressee-report": [
        "01-impl-report.md: IMPL report with substantive actions requires PARENT_DISPATCH_ID to the addressed DISPATCH IMPL relay",
    ],
    "ambiguity/AMB2-none-qualify": [
        "03-dispatch.md: DISPATCH IMPL parent 'amb2' is held by 2 relays (01-noise.md, 02-plan.md); none is an earlier PLAN-REVIEW relay from qi.implementer",
    ],
    "ambiguity/AMB3-latest-wins": [
        "04-dispatch.md: DISPATCH IMPL parent must be an earlier PLAN-REVIEW relay with verdict approve",
    ],
    "p9/P9b-claim-after-scan-blank-line.md": [
        "structurally detectable edit/commit/PR/migration claim lacks ACTIONS_GIT_REF",
    ],
    "rootindex/R-IDX2-decreasing": [
        "INDEX.md: line 4: index time 20260601-110000 precedes the previous row 20260601-120000; an append-only index must be non-decreasing",
    ],
}

EXPECTED_WARN_SET: dict[str, list[str]] = {
    "kr8a/KR8A4-fenced-token-control": [
        "02-report.md: DISPATCH IMPL appears inside fenced code on line 16; quoted/fenced tokens are inert",
    ],
    "ambiguity/AMB3-latest-wins": [
        "04-dispatch.md: 2 relays under 'amb3' qualify as the PLAN-REVIEW parent; selected latest 03-review-mustrevise.md; candidates: 02-review-approve.md",
    ],
}



def load_linter():
    spec = importlib.util.spec_from_file_location("relay_lint", LINT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {LINT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPECTED_ERROR_SET.update(A5_ERRORS)


def main() -> int:
    if "--neutral-cwd" in sys.argv[1:]:
        # CWD-independence control (v29-detection-lp2-cwd-repair): run the
        # whole suite from a fresh empty directory so no expectation can
        # lean on the invoker's CWD. LP2's environment-dependent positive
        # control is the class this catches.
        os.chdir(tempfile.mkdtemp(prefix="relay-fixtures-neutral."))
    lint = load_linter()
    failed = False
    for kind, rel, expected in EXPECTED:
        target = FIXTURES / rel
        cwd_rel = A5_CWD.get(rel)
        seam_rel = A5_SEAM.get(rel)
        old_cwd = os.getcwd()
        if cwd_rel:
            os.chdir(FIXTURES / cwd_rel)
        if seam_rel:
            os.chmod(FIXTURES / seam_rel, 0)
            if os.access(FIXTURES / seam_rel, os.R_OK):
                os.chmod(FIXTURES / seam_rel, 0o644)
                os.chdir(old_cwd)
                print(f"{rel}: read seam ineffective in this environment (root?) FAIL")
                failed = True
                continue
        try:
            if kind == "file":
                result = lint.lint_file(target)
            elif kind == "template":
                result = lint.lint_file(target, template_mode=True)
            elif kind == "index":
                result = lint.lint_relay_index(target / "INDEX.md")
            elif kind == "index-rel":
                result = lint.lint_relay_index(Path("INDEX.md"))
            else:
                result = lint.lint_relay_root(target)
        finally:
            if seam_rel:
                os.chmod(FIXTURES / seam_rel, 0o644)
            os.chdir(old_cwd)
        observed = 0 if result.ok else 1
        expected_errors = EXPECTED_ERROR_SET.get(rel)
        if expected_errors is not None:
            observed_errors = sorted(result.errors)
            expected_sorted = sorted(expected_errors)
            ok = observed == expected and observed_errors == expected_sorted
        else:
            ok = observed == expected
        expected_warns = EXPECTED_WARN_SET.get(rel)
        if expected_warns is not None:
            ok = ok and sorted(result.warnings) == sorted(expected_warns)
        failed = failed or not ok
        label = f"--relay-root {rel}" if kind == "root" else f"--index {rel}" if kind == "index" else rel
        print(f"{label}: expected={expected} observed={observed} {'PASS' if ok else 'FAIL'}")
        if expected_errors is not None:
            print(f"  expected_errors={len(expected_errors)} observed_errors={len(result.errors)}")
        if not ok:
            for err in result.errors:
                print(f"  ERROR {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
