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

LIFECYCLE_EXPECTED = [
    ("root", "mastertier/MT34-foreign-design-kind-present", 0),
    ("root", "mastertier/MT35-foreign-design-kind-omitted", 0),
    ("root", "mastertier/MT36-foreign-audit-pair", 0),
    ("root", "mastertier/MT37-pair-route-control", 0),
    ("root", "mastertier/MT38-pair-foreign-collision", 1),
    ("root", "mastertier/MT39-two-foreign-owners", 1),
    ("root", "mastertier/MT40-master-no-origin", 1),
    ("root", "mastertier/MT41-pair-no-origin-control", 0),
    ("root", "mastertier/MT42-later-origin-control", 0),
    ("root", "mastertier/MT43-same-owner-revisions", 0),
    ("root", "mastertier/MT44-same-position-origin-ambiguity", 1),
    ("root", "mastertier/MT45-reviewer-design-origin", 1),
    ("root", "mastertier/MT46-reviewer-audit-origin", 1),
    ("root", "mastertier/MT47-origin-kind-missing", 1),
    ("root", "mastertier/MT48-approval-kind-missing", 1),
    ("root", "mastertier/MT49-approval-kind-mismatch", 1),
    ("root", "mastertier/MT50-consumer-kind-mismatch", 1),
    ("root", "mastertier/MT51-cross-owner-reviewer", 1),
    ("root", "mastertier/MT52-latest-origin-unreviewed", 1),
    ("root", "mastertier/MT53-latest-origin-must-revise", 1),
    ("root", "mastertier/MT54-latest-review-no-prefilter", 1),
    ("root", "mastertier/MT55-review-must-parent-latest-revision", 1),
    ("root", "mastertier/MT56-later-approval-refused", 1),
    ("root", "mastertier/MT57-invented-reviewer-lock", 1),
    ("root", "mastertier/MT58-same-position-review-ambiguity", 1),
    ("root", "mastertier/MT59-all-seat-pair-planner", 0),
    ("root", "mastertier/MT59-all-seat-pair-implementer", 0),
    ("root", "mastertier/MT59-all-seat-master-planner", 0),
    ("root", "mastertier/MT59-all-seat-master-reviewer", 0),
    ("root", "mastertier/MT59-all-seat-domain-planner", 0),
    ("root", "mastertier/MT59-all-seat-domain-reviewer", 0),
    ("root", "mastertier/MT60-consumer-lock-conflict", 1),
    ("root", "mastertier/MT61-consumer-kind-conflict", 1),
    ("root", "mastertier/MT62-origin-authority-conflict", 1),
    ("root", "mastertier/MT63-review-verdict-conflict", 1),
    ("root", "mastertier/MT64-direct-origin-pair-control", 0),
    ("root", "mastertier/MT65-design-kind-wrong-origin-shape", 1),
    ("root", "mastertier/MT66-audit-kind-wrong-origin-shape", 1),
    ("root", "mastertier/MT67-pair-invented-design-doc", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-pair-planner", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-pair-implementer", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-master-planner", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-master-reviewer", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-domain-planner", 1),
    ("root", "mastertier/MT68-all-seat-missing-review-domain-reviewer", 1),
    ("root", "mastertier/MT69-later-review-parent-conflict-parent-first", 1),
    ("root", "mastertier/MT70-later-review-parent-conflict-other-first", 1),
    ("root", "mastertier/MT71-origin-doc-conflict-lock-first", 1),
    ("root", "mastertier/MT72-origin-doc-conflict-other-first", 1),
    ("root", "mastertier/MT73-origin-phase-conflict-design-first", 1),
    ("root", "mastertier/MT74-origin-phase-conflict-audit-first", 1),
    ("root", "mastertier/MT75-pair-conflicted-foreign-collision", 1),
    ("root", "mastertier/MT76-origin-from-conflict", 1),
]

COMMISSION_PASS = {
    "CM77-valid-chain",
    "CM85-id-reference-inert",
    "CM92-unmarked-same-id-control",
    "CM110-unmarked-grant-control",
    "CM115-auth-reissue-pass",
    "CM116-later-auth-inert",
    "CM117-unmarked-review-excluded",
    "CM118-grant-reissue-pass",
    "CM119-later-grant-inert",
    "CM120-unmarked-review-grant-excluded",
    "CM121-prior-receipt-excluded",
    "CM125-charter-owner-mixedcase-pass",
    "CM127-grant-owner-mixedcase-pass",
    "CM131-approval-local-shape",
    "CM145-auth-target-legacy-pass",
    "CM146-auth-target-explicit-pass",
    "CM151-grant-target-pass",
    "CM152-grant-target-pass",
    "CM179-charter-reissue-pass",
    "CM181-later-charter-inert",
    "CM190-unmarked-universe-controls",
    "CM210-review-reissue-pass",
    "CM219-direct-operator-control",
    "CM220-direct-orchestrator-control",
    "CM245-grammar-positive-1",
    "CM246-grammar-positive-2",
    "CM247-grammar-positive-3",
    "CM248-grammar-positive-4",
    "CM249-grammar-positive-5",
    "CM250-grammar-positive-6",
    "CM251-grammar-positive-7",
    "CM252-grammar-positive-8",
    "CM261-display-present",
    "CM262-display-malformed",
    "CM263-display-repeated",
    "CM264-display-conflicting",
}
COMMISSION_MEMBERS = (
    "CM77-valid-chain", "CM78-auth-uppercase", "CM79-auth-wrong-phase",
    "CM80-auth-wrong-authority", "CM81-auth-wrong-grantor", "CM82-auth-wrong-to",
    "CM83-auth-nonpair-commission-to", "CM84-both-markers-delegation",
    "CM85-id-reference-inert", "CM86-partial-correct-seat", "CM87-empty-correct-seat",
    "CM88-wrong-seat-receipt", "CM89-charter-wrong-authority", "CM90-charter-missing-kind",
    "CM91-auth-shadow-no", "CM92-unmarked-same-id-control",
    "CM93-auth-membership-conflict-match-first", "CM94-auth-membership-conflict-match-last",
    "CM95-auth-same-position", "CM96-charter-equality-commission-scope",
    "CM97-charter-equality-commission-to", "CM98-charter-equality-charter-doc-id",
    "CM99-approval-equality-commission-id", "CM100-approval-equality-commission-scope",
    "CM101-approval-equality-commission-to", "CM102-approval-equality-charter-doc-id",
    "CM103-grant-equality-commission-id", "CM104-grant-equality-commission-scope",
    "CM105-grant-equality-commission-to", "CM106-grant-equality-charter-doc-id",
    "CM107-grant-nonpair-target", "CM108-grant-to-mismatch",
    "CM109-malformed-grant-shadow", "CM110-unmarked-grant-control",
    "CM111-grant-membership-conflict-match-first", "CM112-grant-membership-conflict-match-last",
    "CM113-grant-same-position", "CM114-half-carrier", "CM115-auth-reissue-pass",
    "CM116-later-auth-inert", "CM117-unmarked-review-excluded", "CM118-grant-reissue-pass",
    "CM119-later-grant-inert", "CM120-unmarked-review-grant-excluded",
    "CM121-prior-receipt-excluded", "CM122-grant-marker-yes", "CM123-grant-marker-true",
    "CM124-charter-owner-mismatch", "CM125-charter-owner-mixedcase-pass",
    "CM126-grant-owner-mismatch", "CM127-grant-owner-mixedcase-pass",
    "CM128-approval-local-shape", "CM129-approval-local-shape",
    "CM130-approval-local-shape", "CM131-approval-local-shape",
    "CM132-approval-local-shape", "CM133-wrong-seat-missing-commission-id",
    "CM134-wrong-seat-missing-commission-scope", "CM135-wrong-seat-missing-commission-to",
    "CM136-wrong-seat-missing-charter-doc-id", "CM137-wrong-seat-empty-commission-id",
    "CM138-wrong-seat-empty-commission-scope", "CM139-wrong-seat-empty-commission-to",
    "CM140-wrong-seat-empty-charter-doc-id", "CM141-auth-target-special",
    "CM142-auth-target-master", "CM143-auth-target-reviewer", "CM144-auth-target-multiple",
    "CM145-auth-target-legacy-pass", "CM146-auth-target-explicit-pass",
    "CM147-grant-target-special", "CM148-grant-target-master",
    "CM149-grant-target-reviewer", "CM150-grant-target-multiple",
    "CM151-grant-target-pass", "CM152-grant-target-pass",
    "CM153-byte-exact-address-surface", "CM154-empty-carrier-engages",
    "CM155-empty-carrier-engages", "CM156-empty-carrier-engages",
    "CM157-empty-carrier-engages", "CM158-charter-equality-commission-id",
    "CM159-empty-commission-id-engages",
    "CM160-auth-target-operator-legacy", "CM161-auth-target-operator-explicit",
    "CM162-auth-target-orchestrator-legacy", "CM163-auth-target-orchestrator-explicit",
    "CM164-grant-target-operator-legacy", "CM165-grant-target-operator-explicit",
    "CM166-grant-target-orchestrator-legacy", "CM167-grant-target-orchestrator-explicit",
    "CM168-s1a-charter-parent-absent", "CM169-s1b-charter-parent-wrong",
    "CM170-s1c-charter-parent-ambiguous", "CM171-s1d-charter-parent-nonstage",
    "CM172-auth-reselection-approval", "CM173-auth-reselection-grant",
    "CM174-auth-reselection-receipt", "CM175-approval-parent-absent",
    "CM176-approval-parent-wrong", "CM177-approval-parent-ambiguous",
    "CM178-approval-parent-nonstage", "CM179-charter-reissue-pass",
    "CM180-charter-same-position", "CM181-later-charter-inert",
    "CM182-wrong-owner-reviewer", "CM183-later-must-revise-shadow",
    "CM184-grant-parent-absent", "CM185-grant-parent-wrong",
    "CM186-grant-parent-ambiguous", "CM187-grant-parent-nonstage",
    "CM188-later-approval-inert", "CM189-receipt-missing-grant",
    "CM190-unmarked-universe-controls", "CM191-charter-parent-conflict-parent-first",
    "CM192-charter-parent-conflict-other-first", "CM193-approval-parent-conflict-parent-first",
    "CM194-approval-parent-conflict-other-first", "CM195-grant-parent-conflict-parent-first",
    "CM196-grant-parent-conflict-other-first", "CM197-approval-id-conflict-id-first",
    "CM198-approval-id-conflict-other-first",
    "CM199-grant-wrong-owner-reviewer", "CM200-receipt-wrong-owner-reviewer",
    "CM201-receipt-grant-parent-absent", "CM202-receipt-grant-parent-wrong",
    "CM203-receipt-grant-parent-ambiguous", "CM204-receipt-grant-parent-nonstage",
    "CM205-receipt-approval-parent-absent", "CM206-receipt-approval-parent-wrong",
    "CM207-receipt-approval-parent-ambiguous", "CM208-receipt-approval-parent-nonstage",
    "CM209-receipt-later-grant-inert", "CM210-review-reissue-pass",
    "CM211-review-same-position",
    "CM212-auth-charter-same-position", "CM213-charter-approval-same-position",
    "CM214-approval-grant-same-position", "CM215-grant-receipt-same-position",
    "CM216-malformed-latest-charter", "CM217-wrong-parent-review-shadow",
    "CM218-receipt-missing-charter",
    "CM219-direct-operator-control", "CM220-direct-orchestrator-control",
    "CM221-direct-half-commission-authorization", "CM222-direct-half-commission-id",
    "CM223-direct-half-commission-scope", "CM224-direct-half-commission-to",
    "CM225-direct-half-charter-doc-id", "CM226-pair-receipt-self-grant",
    "CM227-receiving-master-planner", "CM228-receiving-master-reviewer",
    "CM229-receiving-domain-planner", "CM230-receiving-domain-reviewer",
    "CM231-receiving-dda-conflict-yes-first", "CM232-receiving-dda-conflict-yes-last",
    "CM233-receiving-to-conflict-master-first", "CM234-receiving-to-conflict-master-last",
    "CM235-conflict-authorization-at-charter", "CM236-conflict-authorization-at-approval",
    "CM237-conflict-authorization-at-grant", "CM238-conflict-authorization-at-receipt",
    "CM239-conflict-charter-at-approval", "CM240-conflict-charter-at-grant",
    "CM241-conflict-charter-at-receipt", "CM242-conflict-review-at-grant",
    "CM243-conflict-review-at-receipt", "CM244-conflict-grant-at-receipt",
    "CM245-grammar-positive-1", "CM246-grammar-positive-2",
    "CM247-grammar-positive-3", "CM248-grammar-positive-4",
    "CM249-grammar-positive-5", "CM250-grammar-positive-6",
    "CM251-grammar-positive-7", "CM252-grammar-positive-8",
    "CM253-grammar-negative-leading-hyphen", "CM254-grammar-negative-leading-uppercase",
    "CM255-grammar-negative-tail-uppercase", "CM256-grammar-negative-underscore",
    "CM257-grammar-negative-empty", "CM258-identity-wrong-ch-form",
    "CM259-identity-charter-design-mismatch", "CM260-identity-composition-spelling",
    "CM261-display-present", "CM262-display-malformed",
    "CM263-display-repeated", "CM264-display-conflicting",
    "CM265-receiving-mixed-list", "CM266-receiving-identical-repeats",
)
COMMISSION_EXPECTED = [
    ("root", f"mastertier/{name}", 0 if name in COMMISSION_PASS else 1)
    for name in COMMISSION_MEMBERS
]

EXPECTED = A5_EXPECTED + LIFECYCLE_EXPECTED + COMMISSION_EXPECTED + [
    ("file", "mastertier/MT0-generator-smoke.md", 0),
    ("file", "mastertier/MT21-dispatch-impl-master-planner.md", 1),
    ("file", "mastertier/MT21-dispatch-impl-master-reviewer.md", 1),
    ("file", "mastertier/MT21-dispatch-impl-domain-planner.md", 1),
    ("file", "mastertier/MT21-dispatch-impl-domain-reviewer.md", 1),
    ("file", "mastertier/MT21-dispatch-impl-control.md", 0),
    ("file", "mastertier/MT22-dispatch-merge-master-planner.md", 1),
    ("file", "mastertier/MT22-dispatch-merge-master-reviewer.md", 1),
    ("file", "mastertier/MT22-dispatch-merge-domain-planner.md", 1),
    ("file", "mastertier/MT22-dispatch-merge-domain-reviewer.md", 1),
    ("file", "mastertier/MT22-dispatch-merge-control.md", 0),
    ("file", "mastertier/MT23-direct-override-master-planner.md", 1),
    ("file", "mastertier/MT23-direct-override-master-reviewer.md", 1),
    ("file", "mastertier/MT23-direct-override-domain-planner.md", 1),
    ("file", "mastertier/MT23-direct-override-domain-reviewer.md", 1),
    ("file", "mastertier/MT23-direct-override-control.md", 0),
    ("template", "mastertier/MTT21-dispatch-impl-master-planner.md", 1),
    ("template", "mastertier/MTT21-dispatch-impl-master-reviewer.md", 1),
    ("template", "mastertier/MTT21-dispatch-impl-domain-planner.md", 1),
    ("template", "mastertier/MTT21-dispatch-impl-domain-reviewer.md", 1),
    ("template", "mastertier/MTT21-dispatch-impl-control.md", 0),
    ("template", "mastertier/MTT22-dispatch-merge-master-planner.md", 1),
    ("template", "mastertier/MTT22-dispatch-merge-master-reviewer.md", 1),
    ("template", "mastertier/MTT22-dispatch-merge-domain-planner.md", 1),
    ("template", "mastertier/MTT22-dispatch-merge-domain-reviewer.md", 1),
    ("template", "mastertier/MTT22-dispatch-merge-control.md", 0),
    ("template", "mastertier/MTT23-direct-override-master-planner.md", 1),
    ("template", "mastertier/MTT23-direct-override-master-reviewer.md", 1),
    ("template", "mastertier/MTT23-direct-override-domain-planner.md", 1),
    ("template", "mastertier/MTT23-direct-override-domain-reviewer.md", 1),
    ("template", "mastertier/MTT23-direct-override-control.md", 0),
    ("file", "mastertier/MT24-conflict-authority.md", 1),
    ("file", "mastertier/MT24-conflict-phase.md", 1),
    ("file", "mastertier/MT24-conflict-from.md", 1),
    ("file", "mastertier/MT24-conflict-to.md", 1),
    ("file", "mastertier/MT24-conflict-design-doc-id.md", 1),
    ("file", "mastertier/MT24-conflict-design-review-verdict.md", 1),
    ("file", "mastertier/MT24-conflict-dispatch-id.md", 1),
    ("file", "mastertier/MT24-conflict-parent-dispatch-id.md", 1),
    ("file", "mastertier/MT24-conflict-commission-authorization.md", 1),
    ("file", "mastertier/MT24-conflict-commission-id.md", 1),
    ("file", "mastertier/MT24-conflict-commission-scope.md", 1),
    ("file", "mastertier/MT24-conflict-commission-to.md", 1),
    ("file", "mastertier/MT24-conflict-charter-doc-id.md", 1),
    ("file", "mastertier/MT25-a4-conflict-delegated-dispatch-authority.md", 1),
    ("file", "mastertier/MT25-a4-conflict-design-lock-id.md", 1),
    ("file", "mastertier/MT25-a4-conflict-design-record-kind.md", 1),
    ("file", "mastertier/MT26-repeat-authority.md", 0),
    ("file", "mastertier/MT27-repeat-delegated-dispatch-authority.md", 0),
    ("file", "mastertier/MT28-from-launder-master-first.md", 1),
    ("file", "mastertier/MT29-from-launder-master-last.md", 1),
    ("template", "mastertier/MTT28-from-launder-master-first.md", 1),
    ("template", "mastertier/MTT29-from-launder-master-last.md", 1),
    ("template", "mastertier/MTT30-from-launder-master-first.md", 1),
    ("template", "mastertier/MTT31-from-launder-master-last.md", 1),
    ("template", "mastertier/MTT32-from-launder-master-first.md", 1),
    ("template", "mastertier/MTT33-from-launder-master-last.md", 1),
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
    ("file", "rolevocab/RV7e-domain-designlock-kind.md", 0),
    ("file", "rolevocab/RV7e2-domain-designlock-id-only.md", 0),
    ("file", "rolevocab/RV7f-master-delegated-authority.md", 0),
    ("file", "rolevocab/RV7h-delegated-bypass-order.md", 1),
    ("file", "rolevocab/RV7i-lock-empty-first.md", 1),
    ("file", "rolevocab/RV7j-override-then-conflict.md", 1),
    ("file", "rolevocab/RV7k-samevalue-duplicate.md", 0),
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
    ("root", "rolevocab/RV7e-domain-designlock-kind", 0),
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


MASTER_TIER_FIXTURE_ROLES = (
    ("master-planner", "Master Planner"),
    ("master-reviewer", "Master Reviewer"),
    ("domain-planner", "Domain Planner"),
    ("domain-reviewer", "Domain Reviewer"),
)
for _prefix in ("MT2", "MTT2"):
    for _seat, _role in MASTER_TIER_FIXTURE_ROLES:
        EXPECTED_ERROR_SET[f"mastertier/{_prefix}1-dispatch-impl-{_seat}.md"] = [
            f"master-tier seat {_seat!r} may not carry DISPATCH IMPL: execution authority never enters the master tier (DD-v29-master-authority-20260809: token prohibition)",
        ]
        _merge_errors = [
            f"master-tier seat {_seat!r} may not carry DISPATCH MERGE: execution authority never enters the master tier (DD-v29-master-authority-20260809: token prohibition)",
        ]
        if _prefix == "MT2":
            _merge_errors.insert(0, "DISPATCH MERGE FROM must be operator, orchestrator, or an orchestrator-planner-role address")
        EXPECTED_ERROR_SET[f"mastertier/{_prefix}2-dispatch-merge-{_seat}.md"] = _merge_errors
        EXPECTED_ERROR_SET[f"mastertier/{_prefix}3-direct-override-{_seat}.md"] = [
            f"master-tier seat {_seat!r} may not use DESIGN_RECORD_KIND: direct-override: that record kind stays on the operator/orchestrator chain (DD-v29-master-authority-20260809 cross-seat rule 2)",
        ]

_h27_dispatch = "master-tier seat 'master-planner' may not carry DISPATCH IMPL: execution authority never enters the master tier (DD-v29-master-authority-20260809: token prohibition)"
_h27_override = "master-tier seat 'domain-planner' may not use DESIGN_RECORD_KIND: direct-override: that record kind stays on the operator/orchestrator chain (DD-v29-master-authority-20260809 cross-seat rule 2)"
_h27_master_override = "master-tier seat 'master-planner' may not use DESIGN_RECORD_KIND: direct-override: that record kind stays on the operator/orchestrator chain (DD-v29-master-authority-20260809 cross-seat rule 2)"
_a4_lock = "DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"
_a4_kind = "DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present"
_lock_rule = " (DD-v29-master-authority-20260809 cross-seat rule 1)"
EXPECTED_ERROR_SET.update({
    "rolevocab/RV7a-master-dispatch-failclosed.md": [_h27_dispatch],
    "rolevocab/RV7b-domain-direct-override.md": [_h27_override],
    "rolevocab/RV7e-domain-designlock-kind.md": [],
    "rolevocab/RV7e2-domain-designlock-id-only.md": [],
    "rolevocab/RV7f-master-delegated-authority.md": [],
    "rolevocab/RV7k-samevalue-duplicate.md": [],
    "rolevocab/RV7l-override-plus-lock-conflict.md": [_a4_lock, _h27_master_override],
    "rolevocab/RV7m-kind-conflict-plus-lock.md": [_a4_kind],
    "rolevocab/RV7a-master-dispatch-failclosed": [f"RV7a-master-dispatch-failclosed.md: {_h27_dispatch}"],
    "rolevocab/RV7b-domain-direct-override": [
        f"RV7b-domain-direct-override.md: {_h27_override}",
        "RV7b-domain-direct-override.md: master/domain-seat consumer DESIGN_LOCK_ID 'rv7b-doc' has no resolvable earlier master/domain origin" + _lock_rule,
    ],
    "rolevocab/RV7e-domain-designlock-kind": [],
    "rolevocab/RV7e2-domain-designlock-id-only": [
        "01-plan.md: master/domain-seat consumer DESIGN_LOCK_ID 'rv7e2-doc' has no resolvable earlier master/domain origin" + _lock_rule,
    ],
    "rolevocab/RV7f-master-delegated-authority": [
        "01-plan.md: commission machine engaged but relay is not consumed by any Task 9.5a stage (DD-v29-master-authority-20260809 cross-seat rule 3)",
    ],
    "rolevocab/RV7k-samevalue-duplicate": [
        "01-plan.md: commission machine engaged but relay is not consumed by any Task 9.5a stage (DD-v29-master-authority-20260809 cross-seat rule 3)",
    ],
    "rolevocab/RV7l-override-plus-lock-conflict": [f"01-plan.md: {_a4_lock}", f"01-plan.md: {_h27_master_override}"],
    "rolevocab/RV7m-kind-conflict-plus-lock": [f"01-plan.md: {_a4_kind}"],
})

_h27_conflict_fields = (
    "AUTHORITY", "PHASE", "FROM", "TO", "DESIGN_DOC_ID", "DESIGN_REVIEW_VERDICT",
    "DISPATCH_ID", "PARENT_DISPATCH_ID", "COMMISSION_AUTHORIZATION", "COMMISSION_ID",
    "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
)
_h27_rule5_suffix = " (DD-v29-master-authority-20260809 rule 5)"
for _field in _h27_conflict_fields:
    _name = _field.lower().replace("_", "-")
    EXPECTED_ERROR_SET[f"mastertier/MT24-conflict-{_name}.md"] = [
        f"{_field} carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present{_h27_rule5_suffix}",
    ]
for _field in ("DELEGATED_DISPATCH_AUTHORITY", "DESIGN_LOCK_ID", "DESIGN_RECORD_KIND"):
    _name = _field.lower().replace("_", "-")
    EXPECTED_ERROR_SET[f"mastertier/MT25-a4-conflict-{_name}.md"] = [
        f"{_field} carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present",
    ]
_h27_from_conflict = "FROM carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
for _prefix in ("MT2", "MTT2"):
    EXPECTED_ERROR_SET[f"mastertier/{_prefix}8-from-launder-master-first.md"] = [_h27_from_conflict]
    EXPECTED_ERROR_SET[f"mastertier/{_prefix}9-from-launder-master-last.md"] = [_h27_from_conflict]
for _number, _order in ((30, "master-first"), (31, "master-last"), (32, "master-first"), (33, "master-last")):
    EXPECTED_ERROR_SET[f"mastertier/MTT{_number}-from-launder-{_order}.md"] = [_h27_from_conflict]

_lock_conflict = "DESIGN_LOCK_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_kind_conflict = "DESIGN_RECORD_KIND carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_authority_conflict = "AUTHORITY carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_verdict_conflict = "DESIGN_REVIEW_VERDICT carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_parent_conflict = "PARENT_DISPATCH_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_doc_conflict = "DESIGN_DOC_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_phase_conflict = "PHASE carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
for _rel in (
    "MT34-foreign-design-kind-present", "MT35-foreign-design-kind-omitted",
    "MT36-foreign-audit-pair", "MT37-pair-route-control", "MT41-pair-no-origin-control",
    "MT42-later-origin-control", "MT43-same-owner-revisions", "MT64-direct-origin-pair-control",
):
    EXPECTED_ERROR_SET[f"mastertier/{_rel}"] = []
for _seat in ("pair-planner", "pair-implementer", "master-planner", "master-reviewer", "domain-planner", "domain-reviewer"):
    EXPECTED_ERROR_SET[f"mastertier/MT59-all-seat-{_seat}"] = []
EXPECTED_ERROR_SET.update({
    "mastertier/MT38-pair-foreign-collision": [
        "03-consumer.md: DESIGN_LOCK_ID 'lock-c4-collision' resolves to both pair and master/domain origin groups; lock routing fails closed" + _lock_rule,
    ],
    "mastertier/MT39-two-foreign-owners": [
        "03-consumer.md: DESIGN_LOCK_ID 'lock-c4-multiple' resolves to 2 master/domain owner groups; lock routing fails closed" + _lock_rule,
    ],
    "mastertier/MT40-master-no-origin": [
        "01-consumer.md: master/domain-seat consumer DESIGN_LOCK_ID 'lock-c4-master-none' has no resolvable earlier master/domain origin" + _lock_rule,
    ],
    "mastertier/MT44-same-position-origin-ambiguity": [
        "02-consumer.md: DESIGN_LOCK_ID 'lock-c4-origin-tie' has 2 same-position latest origins for owner alpha; lock resolution fails closed" + _lock_rule,
    ],
    "mastertier/MT45-reviewer-design-origin": [
        "03-consumer.md: foreign design-doc may not originate from reviewer seat 'master-reviewer' in PHASE DESIGN" + _lock_rule,
    ],
    "mastertier/MT46-reviewer-audit-origin": [
        "03-consumer.md: foreign audit-record may not originate from reviewer seat 'domain-reviewer' in PHASE AUDIT" + _lock_rule,
    ],
    "mastertier/MT47-origin-kind-missing": [
        "03-consumer.md: foreign origin 01-origin.md requires DESIGN_RECORD_KIND" + _lock_rule,
    ],
    "mastertier/MT48-approval-kind-missing": [
        "03-consumer.md: foreign approval 02-review.md requires DESIGN_RECORD_KIND equal to origin kind 'design-doc'" + _lock_rule,
    ],
    "mastertier/MT49-approval-kind-mismatch": [
        "03-consumer.md: foreign approval 02-review.md DESIGN_RECORD_KIND 'audit-record' does not equal origin kind 'design-doc'" + _lock_rule,
    ],
    "mastertier/MT50-consumer-kind-mismatch": [
        "03-consumer.md: consumer DESIGN_RECORD_KIND 'audit-record' does not equal foreign origin kind 'design-doc'" + _lock_rule,
    ],
    "mastertier/MT51-cross-owner-reviewer": [
        "03-consumer.md: foreign approval 02-review.md must be FROM alpha.master-reviewer, the same-owner tier reviewer" + _lock_rule,
    ],
    "mastertier/MT52-latest-origin-unreviewed": [
        "04-consumer.md: foreign DESIGN_LOCK_ID 'lock-c5-unreviewed-v2' has no earlier DESIGN-REVIEW parented to latest origin 03-origin-v2.md" + _lock_rule,
    ],
    "mastertier/MT53-latest-origin-must-revise": [
        "04-consumer.md: latest foreign approval 03-review-v2.md requires DESIGN_REVIEW_VERDICT: approve; got 'must-revise'" + _lock_rule,
    ],
    "mastertier/MT54-latest-review-no-prefilter": [
        "04-consumer.md: latest foreign approval 03-review-must-revise.md requires DESIGN_REVIEW_VERDICT: approve; got 'must-revise'" + _lock_rule,
    ],
    "mastertier/MT55-review-must-parent-latest-revision": [
        "04-consumer.md: foreign DESIGN_LOCK_ID 'lock-c5-stale-review' has no earlier DESIGN-REVIEW parented to latest origin 03-origin-v2.md" + _lock_rule,
    ],
    "mastertier/MT56-later-approval-refused": [
        "02-consumer.md: foreign DESIGN_LOCK_ID 'lock-c5-later-review' has no earlier DESIGN-REVIEW parented to latest origin 01-origin.md" + _lock_rule,
    ],
    "mastertier/MT57-invented-reviewer-lock": [
        "01-consumer.md: master/domain-seat consumer DESIGN_LOCK_ID 'lock-c5-invented-reviewer' has no resolvable earlier master/domain origin" + _lock_rule,
    ],
    "mastertier/MT58-same-position-review-ambiguity": [
        "03-consumer.md: latest origin 01-origin.md has 2 same-position latest DESIGN-REVIEW candidates; lock resolution fails closed" + _lock_rule,
    ],
    "mastertier/MT60-consumer-lock-conflict": [
        "03-consumer.md: " + _lock_conflict,
    ],
    "mastertier/MT61-consumer-kind-conflict": [
        "03-consumer.md: " + _kind_conflict,
    ],
    "mastertier/MT62-origin-authority-conflict": [
        "01-origin.md: " + _authority_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting AUTHORITY occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT63-review-verdict-conflict": [
        "02-review.md: " + _verdict_conflict,
        "03-consumer.md: foreign approval 02-review.md has conflicting DESIGN_REVIEW_VERDICT occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT65-design-kind-wrong-origin-shape": [
        "03-consumer.md: foreign design-doc origin 01-origin.md must be planner-seat PHASE DESIGN with AUTHORITY: design-only" + _lock_rule,
    ],
    "mastertier/MT66-audit-kind-wrong-origin-shape": [
        "03-consumer.md: foreign audit-record origin 01-origin.md must be planner-seat PHASE AUDIT with AUTHORITY: review-only or report-only" + _lock_rule,
    ],
    "mastertier/MT67-pair-invented-design-doc": [
        "01-consumer.md: DESIGN_LOCK_ID 'lock-c5-pair-invented' has no earlier same-owner DESIGN relay carrying matching DESIGN_DOC_ID",
    ],
    "mastertier/MT69-later-review-parent-conflict-parent-first": [
        "03-review-must-revise.md: " + _parent_conflict,
        "04-consumer.md: foreign approval 03-review-must-revise.md has conflicting PARENT_DISPATCH_ID occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT70-later-review-parent-conflict-other-first": [
        "03-review-must-revise.md: " + _parent_conflict,
        "04-consumer.md: foreign approval 03-review-must-revise.md has conflicting PARENT_DISPATCH_ID occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT71-origin-doc-conflict-lock-first": [
        "01-origin.md: " + _doc_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting DESIGN_DOC_ID occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT72-origin-doc-conflict-other-first": [
        "01-origin.md: " + _doc_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting DESIGN_DOC_ID occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT73-origin-phase-conflict-design-first": [
        "01-origin.md: " + _phase_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting PHASE occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT74-origin-phase-conflict-audit-first": [
        "01-origin.md: " + _phase_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting PHASE occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
    "mastertier/MT75-pair-conflicted-foreign-collision": [
        "02-foreign-origin.md: " + _doc_conflict,
        "03-consumer.md: DESIGN_LOCK_ID 'lock-c8-conflicted-collision' resolves to both pair and master/domain origin groups; lock routing fails closed" + _lock_rule,
    ],
    "mastertier/MT76-origin-from-conflict": [
        "01-origin.md: " + _h27_from_conflict,
        "03-consumer.md: foreign origin 01-origin.md has conflicting FROM occurrences; lock lifecycle refuses first-value resolution" + _lock_rule,
    ],
})
for _seat in ("pair-planner", "pair-implementer", "master-planner", "master-reviewer", "domain-planner", "domain-reviewer"):
    _case = f"c5-missing-review-{_seat}"
    EXPECTED_ERROR_SET[f"mastertier/MT68-all-seat-missing-review-{_seat}"] = [
        f"02-consumer.md: foreign DESIGN_LOCK_ID 'lock-{_case}' has no earlier DESIGN-REVIEW parented to latest origin 01-origin.md" + _lock_rule,
    ]

_commission_rule = " (DD-v29-master-authority-20260809 cross-seat rule 3)"
_commission_auth_rule = " (DD-v29-master-authority-20260809 cross-seat rule 3a)"
_commission_charter_rule = " (DD-v29-master-authority-20260809 cross-seat rule 3b)"
_commission_grant_rule = " (DD-v29-master-authority-20260809 cross-seat rule 3c)"
_commission_receipt_rule = " (DD-v29-master-authority-20260809 cross-seat rule 3d)"
_rule3e = " (DD-v29-master-authority-20260809 cross-seat rule 3e)"
_auth_shape = (
    "relay carries COMMISSION_AUTHORIZATION but fails stage-(a) shape (literal yes, direct-authority FROM, "
    "PHASE PLAN, AUTHORITY plan-only, TO exactly one master-planner, complete equality surface, pair-planner "
    "COMMISSION_TO)" + _commission_auth_rule
)
_unconsumed = "commission machine engaged but relay is not consumed by any Task 9.5a stage" + _commission_rule
_incomplete_receipt = "commissioned pair receipt has incomplete equality surface" + _commission_receipt_rule
for _name in COMMISSION_PASS:
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = []
for _name in (
    "CM78-auth-uppercase", "CM79-auth-wrong-phase", "CM80-auth-wrong-authority",
    "CM81-auth-wrong-grantor", "CM82-auth-wrong-to", "CM83-auth-nonpair-commission-to",
    "CM141-auth-target-special", "CM142-auth-target-master", "CM143-auth-target-reviewer",
    "CM144-auth-target-multiple", "CM160-auth-target-operator-legacy",
    "CM161-auth-target-operator-explicit", "CM162-auth-target-orchestrator-legacy",
    "CM163-auth-target-orchestrator-explicit",
):
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = ["01-auth.md: " + _auth_shape]
EXPECTED_ERROR_SET.update({
    "mastertier/CM84-both-markers-delegation": [
        "01-relay.md: a direct delegation grant carries commission gating carriers; "
        "direct path must carry none of the five" + _commission_rule,
        "01-relay.md: DELEGATED_DISPATCH_AUTHORITY: yes addressed to master/domain seat "
        "'alpha.master-planner'; the tier never receives delegated dispatch authority"
        + _rule3e,
    ],
    "mastertier/CM86-partial-correct-seat": ["01-receipt.md: " + _incomplete_receipt],
    "mastertier/CM87-empty-correct-seat": ["01-receipt.md: " + _incomplete_receipt],
    "mastertier/CM88-wrong-seat-receipt": [
        "01-receipt.md: commissioned pair receipt FROM 'gamma.pair-planner' does not equal COMMISSION_TO 'beta.pair-planner'" + _commission_receipt_rule,
    ],
    "mastertier/CM89-charter-wrong-authority": [
        "02-charter.md: charter fails stage-(b) shape; requires master-planner PHASE DESIGN, AUTHORITY design-only, matching document identity, and DESIGN_RECORD_KIND design-doc" + _commission_charter_rule,
    ],
    "mastertier/CM90-charter-missing-kind": [
        "02-charter.md: charter fails stage-(b) shape; requires master-planner PHASE DESIGN, AUTHORITY design-only, matching document identity, and DESIGN_RECORD_KIND design-doc" + _commission_charter_rule,
    ],
    "mastertier/CM91-auth-shadow-no": [
        "02-auth-no.md: " + _auth_shape,
        "03-charter.md: latest authorization-universe member 02-auth-no.md fails stage-(a) shape; marker-bearing malformed authorization shadows and fails" + _commission_auth_rule,
    ],
})
# The selected-universe conflicts must expose both the relay-local rule-5 refusal
# and the downstream candidate-selection refusal in both occurrence orders.
_cid_conflict = "COMMISSION_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
_commission_surface_fields = ("COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID")
for _number, _order in ((93, "match-first"), (94, "match-last")):
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-auth-membership-conflict-{_order}"] = [
        "01-auth.md: " + _cid_conflict,
        "02-charter.md: selected authorization-universe member 01-auth.md has conflicting COMMISSION_ID occurrences; universe selection refuses first-value resolution" + _commission_auth_rule,
    ]
EXPECTED_ERROR_SET.update({
    "mastertier/CM95-auth-same-position": [
        "02-charter.md: commission 'cm95' has 2 authorization-universe candidates at the latest order position; ambiguity fails closed" + _commission_auth_rule,
    ],
    "mastertier/CM96-charter-equality-commission-scope": [
        "02-charter.md: charter equality surface is not byte-equal to the selected authorization" + _commission_charter_rule,
    ],
    "mastertier/CM97-charter-equality-commission-to": [
        "02-charter.md: charter equality surface is not byte-equal to the selected authorization" + _commission_charter_rule,
    ],
    "mastertier/CM98-charter-equality-charter-doc-id": [
        "02-charter.md: charter equality surface is not byte-equal to the selected authorization" + _commission_charter_rule,
    ],
})
for _number in range(99, 103):
    _field = _commission_surface_fields[_number - 99]
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-approval-equality-{_field.lower().replace('_', '-')}"] = [
        "03-approval.md: approval equality surface is not byte-equal to the selected authorization and charter revision" + _commission_charter_rule,
    ]
for _number in range(103, 107):
    _field = _commission_surface_fields[_number - 103]
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-grant-equality-{_field.lower().replace('_', '-')}"] = [
        "04-grant.md: grant equality surface is not byte-equal across selected authorization, charter revision, approval, and grant" + _commission_grant_rule,
    ]
EXPECTED_ERROR_SET.update({
    "mastertier/CM107-grant-nonpair-target": [
        "04-grant.md: COMMISSION_TO 'beta.pair-implementer' is not exactly one non-special pair-planner address" + _commission_grant_rule,
    ],
    "mastertier/CM108-grant-to-mismatch": [
        "04-grant.md: grant TO must be exactly the COMMISSION_TO address" + _commission_grant_rule,
    ],
    "mastertier/CM109-malformed-grant-shadow": [
        "05-grant-no.md: master-planner grant requires literal DELEGATED_DISPATCH_AUTHORITY: yes; got 'no'" + _commission_grant_rule,
        "06-receipt.md: latest grant-universe member 05-grant-no.md fails stage-(c) shape; marker-bearing malformed grant shadows and fails" + _commission_grant_rule,
    ],
})
for _number, _order in ((111, "match-first"), (112, "match-last")):
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-grant-membership-conflict-{_order}"] = [
        "05-grant-conflict.md: " + _cid_conflict,
        "06-receipt.md: selected grant-universe member 05-grant-conflict.md has conflicting COMMISSION_ID occurrences; universe selection refuses first-value resolution" + _commission_receipt_rule,
    ]
EXPECTED_ERROR_SET.update({
    "mastertier/CM113-grant-same-position": [
        "06-receipt.md: commission 'cm113' has 2 grant-universe candidates at the latest order position; ambiguity fails closed" + _commission_receipt_rule,
    ],
    "mastertier/CM114-half-carrier": ["01-relay.md: " + _unconsumed],
    "mastertier/CM122-grant-marker-yes": [
        "05-grant-malformed.md: master-planner grant requires literal DELEGATED_DISPATCH_AUTHORITY: yes; got 'YES'" + _commission_grant_rule,
        "06-receipt.md: latest grant-universe member 05-grant-malformed.md fails stage-(c) shape; marker-bearing malformed grant shadows and fails" + _commission_grant_rule,
    ],
    "mastertier/CM123-grant-marker-true": [
        "05-grant-malformed.md: master-planner grant requires literal DELEGATED_DISPATCH_AUTHORITY: yes; got 'true'" + _commission_grant_rule,
        "06-receipt.md: latest grant-universe member 05-grant-malformed.md fails stage-(c) shape; marker-bearing malformed grant shadows and fails" + _commission_grant_rule,
    ],
    "mastertier/CM124-charter-owner-mismatch": [
        "02-charter.md: charter FROM must equal the master-planner address selected authorization TO" + _commission_charter_rule,
    ],
    "mastertier/CM126-grant-owner-mismatch": [
        "04-grant.md: grant FROM must equal the master-planner address selected authorization TO" + _commission_grant_rule,
    ],
    "mastertier/CM128-approval-local-shape": [
        "03-approval.md: charter approval requires DESIGN_RECORD_KIND: design-doc and a grammatical DESIGN_REVIEW_VERDICT" + _commission_charter_rule,
    ],
    "mastertier/CM129-approval-local-shape": [
        "03-approval.md: charter approval requires DESIGN_RECORD_KIND: design-doc and a grammatical DESIGN_REVIEW_VERDICT" + _commission_charter_rule,
    ],
    "mastertier/CM130-approval-local-shape": [
        "03-approval.md: charter approval requires DESIGN_RECORD_KIND: design-doc and a grammatical DESIGN_REVIEW_VERDICT" + _commission_charter_rule,
    ],
    "mastertier/CM132-approval-local-shape": [
        "03-approval.md: DESIGN_REVIEW_VERDICT has non-canonical value: 'banana'",
        "03-approval.md: charter approval requires DESIGN_RECORD_KIND: design-doc and a grammatical DESIGN_REVIEW_VERDICT" + _commission_charter_rule,
    ],
})
EXPECTED_ERROR_SET["mastertier/CM79-auth-wrong-phase"] = [
    "01-auth.md: phase/authority inconsistent: PHASE=SITREP, AUTHORITY='plan-only'",
    "01-auth.md: " + _auth_shape,
]
for _number in range(133, 141):
    _mode = "missing" if _number < 137 else "empty"
    _field = _commission_surface_fields[(_number - 133) % 4]
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-wrong-seat-{_mode}-{_field.lower().replace('_', '-')}"] = [
        "01-receipt.md: " + _incomplete_receipt,
    ]
for _number, (_label, _target) in enumerate(
    (("special", "operator"), ("master", "beta.master-planner"), ("reviewer", "beta.reviewer"),
     ("multiple", "beta.pair-planner, gamma.pair-planner")), start=147,
):
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-grant-target-{_label}"] = [
        f"04-grant.md: COMMISSION_TO {_target!r} is not exactly one non-special pair-planner address" + _commission_grant_rule,
    ]
EXPECTED_ERROR_SET["mastertier/CM148-grant-target-master"].append(
    "04-grant.md: DELEGATED_DISPATCH_AUTHORITY: yes addressed to master/domain seat "
    "'beta.master-planner'; the tier never receives delegated dispatch authority" + _rule3e
)

_auth_no_shape = (
    "relay carries COMMISSION_AUTHORIZATION but fails stage-(a) shape (literal yes, direct-authority FROM, "
    "PHASE PLAN, AUTHORITY plan-only, TO exactly one master-planner, complete equality surface, pair-planner "
    "COMMISSION_TO)" + _commission_auth_rule
)
_parent_conflict = (
    "PARENT_DISPATCH_ID carries 2 distinct values across 2 occurrences; an authority-critical field is "
    "fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix
)
_dispatch_conflict = (
    "DISPATCH_ID carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed "
    "unless exactly one distinct value is present" + _h27_rule5_suffix
)
EXPECTED_ERROR_SET.update({
    "mastertier/CM168-s1a-charter-parent-absent": [
        "02-charter.md: charter PARENT_DISPATCH_ID is mandatory and must resolve the selected authorization" + _commission_charter_rule,
    ],
    "mastertier/CM169-s1b-charter-parent-wrong": [
        "03-charter.md: charter parent 'cm169-auth-v1' does not equal selected authorization 'cm169-auth-v2'" + _commission_charter_rule,
    ],
    "mastertier/CM170-s1c-charter-parent-ambiguous": [
        "02-charter.md: charter parent 'cm170-auth' has 2 same-position latest holders; ambiguity fails closed" + _commission_charter_rule,
    ],
    "mastertier/CM171-s1d-charter-parent-nonstage": [
        "03-charter.md: charter parent 'cm171-auth' resolves to 02-decoy.md, which is not the selected stage-(a) authorization" + _commission_charter_rule,
    ],
    "mastertier/CM172-auth-reselection-approval": [
        "03-auth-no.md: " + _auth_no_shape,
        "04-approval.md: latest authorization-universe member 03-auth-no.md fails stage-(a) shape; marker-bearing malformed authorization shadows and fails" + _commission_auth_rule,
    ],
    "mastertier/CM173-auth-reselection-grant": [
        "04-auth-no.md: " + _auth_no_shape,
        "05-grant.md: latest authorization-universe member 04-auth-no.md fails stage-(a) shape; marker-bearing malformed authorization shadows and fails" + _commission_auth_rule,
    ],
    "mastertier/CM174-auth-reselection-receipt": [
        "05-auth-no.md: " + _auth_no_shape,
        "06-receipt.md: latest authorization-universe member 05-auth-no.md fails stage-(a) shape; marker-bearing malformed authorization shadows and fails" + _commission_auth_rule,
    ],
    "mastertier/CM175-approval-parent-absent": [
        "03-approval.md: charter approval PARENT_DISPATCH_ID is mandatory and must resolve the latest charter revision" + _commission_charter_rule,
    ],
    "mastertier/CM176-approval-parent-wrong": [
        "04-approval.md: charter approval parent 'cm176-status' resolves to 03-status.md, which is not the latest charter revision" + _commission_charter_rule,
    ],
    "mastertier/CM177-approval-parent-ambiguous": [
        "03-approval.md: charter approval parent 'cm177-charter' has 2 same-position latest holders; ambiguity fails closed" + _commission_charter_rule,
    ],
    "mastertier/CM178-approval-parent-nonstage": [
        "04-approval.md: charter approval parent 'cm178-charter' resolves to 03-decoy.md, which is not the latest charter revision" + _commission_charter_rule,
    ],
    "mastertier/CM180-charter-same-position": [
        "03-approval.md: commission 'cm180' has 2 charter revisions at the latest order position; ambiguity fails closed" + _commission_charter_rule,
    ],
    "mastertier/CM182-wrong-owner-reviewer": [
        "03-approval.md: charter approval must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_charter_rule,
    ],
    "mastertier/CM183-later-must-revise-shadow": [
        "05-grant.md: latest charter review 04-review.md requires DESIGN_REVIEW_VERDICT: approve; got 'must-revise'" + _commission_grant_rule,
    ],
    "mastertier/CM184-grant-parent-absent": [
        "04-grant.md: grant PARENT_DISPATCH_ID is mandatory and must resolve the latest approving review" + _commission_grant_rule,
    ],
    "mastertier/CM185-grant-parent-wrong": [
        "05-grant.md: grant parent 'cm185-status' resolves to 04-status.md, which is not the latest charter review" + _commission_grant_rule,
    ],
    "mastertier/CM186-grant-parent-ambiguous": [
        "04-grant.md: grant parent 'cm186-approval' has 2 same-position latest holders; ambiguity fails closed" + _commission_grant_rule,
    ],
    "mastertier/CM187-grant-parent-nonstage": [
        "05-grant.md: grant parent 'cm187-approval' resolves to 04-decoy.md, which is not the latest charter review" + _commission_grant_rule,
    ],
    "mastertier/CM188-later-approval-inert": [
        "03-grant.md: grant has no earlier charter review for the latest charter revision" + _commission_grant_rule,
    ],
    "mastertier/CM189-receipt-missing-grant": [
        "04-receipt.md: commission 'cm189' has no earlier grant-universe member" + _commission_receipt_rule,
    ],
    "mastertier/CM191-charter-parent-conflict-parent-first": ["02-charter.md: " + _parent_conflict],
    "mastertier/CM192-charter-parent-conflict-other-first": ["02-charter.md: " + _parent_conflict],
    "mastertier/CM193-approval-parent-conflict-parent-first": ["03-approval.md: " + _parent_conflict],
    "mastertier/CM194-approval-parent-conflict-other-first": ["03-approval.md: " + _parent_conflict],
    "mastertier/CM195-grant-parent-conflict-parent-first": ["04-grant.md: " + _parent_conflict],
    "mastertier/CM196-grant-parent-conflict-other-first": ["04-grant.md: " + _parent_conflict],
    "mastertier/CM197-approval-id-conflict-id-first": [
        "03-approval.md: " + _dispatch_conflict,
        "04-grant.md: selected charter review 03-approval.md has conflicting DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution" + _commission_grant_rule,
    ],
    "mastertier/CM198-approval-id-conflict-other-first": [
        "03-approval.md: " + _dispatch_conflict,
        "04-grant.md: selected charter review 03-approval.md has conflicting DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution" + _commission_grant_rule,
    ],
    "mastertier/CM199-grant-wrong-owner-reviewer": [
        "03-approval.md: charter approval must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_charter_rule,
        "04-grant.md: charter review must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_grant_rule,
    ],
    "mastertier/CM200-receipt-wrong-owner-reviewer": [
        "03-approval.md: charter approval must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_charter_rule,
        "04-grant.md: charter review must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_grant_rule,
        "05-receipt.md: charter review must be FROM alpha.master-reviewer, the charter owner's tier reviewer" + _commission_receipt_rule,
    ],
    "mastertier/CM201-receipt-grant-parent-absent": [
        "04-grant.md: grant PARENT_DISPATCH_ID is mandatory and must resolve the latest approving review" + _commission_grant_rule,
        "05-receipt.md: grant PARENT_DISPATCH_ID is mandatory and must resolve an approving review" + _commission_receipt_rule,
    ],
    "mastertier/CM202-receipt-grant-parent-wrong": [
        "04-grant.md: grant parent 'cm202-auth' resolves to 01-auth.md, which is not the latest charter review" + _commission_grant_rule,
        "05-receipt.md: selected grant does not parent to the latest charter review before the grant" + _commission_receipt_rule,
    ],
    "mastertier/CM203-receipt-grant-parent-ambiguous": [
        "04-grant.md: grant parent 'cm203-approval' has 2 same-position latest holders; ambiguity fails closed" + _commission_grant_rule,
        "05-receipt.md: grant parent 'cm203-approval' has 2 same-position latest holders; ambiguity fails closed" + _commission_receipt_rule,
    ],
    "mastertier/CM204-receipt-grant-parent-nonstage": [
        "05-grant.md: grant parent 'cm204-approval' resolves to 04-decoy.md, which is not the latest charter review" + _commission_grant_rule,
        "06-receipt.md: selected grant does not parent to the latest charter review before the grant" + _commission_receipt_rule,
    ],
    "mastertier/CM205-receipt-approval-parent-absent": [
        "03-approval.md: charter approval PARENT_DISPATCH_ID is mandatory and must resolve the latest charter revision" + _commission_charter_rule,
        "04-grant.md: selected charter review 03-approval.md lacks a usable PARENT_DISPATCH_ID" + _commission_grant_rule,
        "05-receipt.md: selected charter review 03-approval.md lacks a usable PARENT_DISPATCH_ID" + _commission_receipt_rule,
    ],
    "mastertier/CM206-receipt-approval-parent-wrong": [
        "03-approval.md: charter approval parent 'cm206-auth' resolves to 01-auth.md, which is not the latest charter revision" + _commission_charter_rule,
        "04-grant.md: approval parent 'cm206-auth' resolves to 01-auth.md, which is not the charter revision" + _commission_grant_rule,
        "05-receipt.md: approval parent 'cm206-auth' does not resolve to the latest charter revision before the review" + _commission_receipt_rule,
    ],
    "mastertier/CM207-receipt-approval-parent-ambiguous": [
        "03-approval.md: charter approval parent 'cm207-charter' has 2 same-position latest holders; ambiguity fails closed" + _commission_charter_rule,
        "04-grant.md: approval parent 'cm207-charter' has 2 same-position latest holders; ambiguity fails closed" + _commission_grant_rule,
        "05-receipt.md: approval parent 'cm207-charter' has 2 same-position latest holders; ambiguity fails closed" + _commission_receipt_rule,
    ],
    "mastertier/CM208-receipt-approval-parent-nonstage": [
        "04-approval.md: charter approval parent 'cm208-charter' resolves to 03-decoy.md, which is not the latest charter revision" + _commission_charter_rule,
        "05-grant.md: approval parent 'cm208-charter' resolves to 03-decoy.md, which is not the charter revision" + _commission_grant_rule,
        "06-receipt.md: approval parent 'cm208-charter' does not resolve to the latest charter revision before the review" + _commission_receipt_rule,
    ],
    "mastertier/CM209-receipt-later-grant-inert": [
        "04-receipt.md: commission 'cm209' has no earlier grant-universe member" + _commission_receipt_rule,
    ],
    "mastertier/CM211-review-same-position": [
        "04-grant.md: commission 'cm211' has 2 charter reviews at the latest order position; ambiguity fails closed" + _commission_grant_rule,
    ],
    "mastertier/CM212-auth-charter-same-position": [
        "b/01-charter.md: commission 'cm212' has no earlier authorization-universe member" + _commission_charter_rule,
    ],
    "mastertier/CM213-charter-approval-same-position": [
        "b/02-approval.md: charter approval has no earlier charter revision" + _commission_charter_rule,
    ],
    "mastertier/CM214-approval-grant-same-position": [
        "b/03-grant.md: grant has no earlier charter review for the latest charter revision" + _commission_grant_rule,
    ],
    "mastertier/CM215-grant-receipt-same-position": [
        "b/04-receipt.md: commission 'cm215' has no earlier grant-universe member" + _commission_receipt_rule,
    ],
    "mastertier/CM216-malformed-latest-charter": [
        "03-charter-malformed.md: commission machine engaged but relay is not consumed by any Task 9.5a stage" + _commission_rule,
        "04-approval.md: charter approval parent 'cm216-charter' does not equal latest charter revision 'cm216-charter-bad'" + _commission_charter_rule,
        "05-grant.md: selected latest charter revision 03-charter-malformed.md fails stage-(b) shape" + _commission_grant_rule,
        "06-receipt.md: selected latest charter revision 03-charter-malformed.md fails stage-(b) shape" + _commission_receipt_rule,
    ],
    "mastertier/CM217-wrong-parent-review-shadow": [
        "04-review-wrong.md: charter approval parent 'cm217-auth' resolves to 01-auth.md, which is not the latest charter revision" + _commission_charter_rule,
        "05-grant.md: grant parent 'cm217-review-ok' does not equal latest charter review 'cm217-review-wrong'" + _commission_grant_rule,
        "06-receipt.md: selected grant does not parent to the latest charter review before the grant" + _commission_receipt_rule,
    ],
    "mastertier/CM218-receipt-missing-charter": [
        "02-grant.md: grant lacks an earlier charter revision" + _commission_grant_rule,
        "03-receipt.md: selected latest charter revision <none> fails stage-(b) shape" + _commission_receipt_rule,
    ],
})

_direct_insulation = (
    "a direct delegation grant carries commission gating carriers; direct path must carry none of the five"
    + _commission_rule
)
for _number in range(221, 226):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = ["01-grant.md: " + _direct_insulation]

_receiving = (
    "DELEGATED_DISPATCH_AUTHORITY: yes addressed to master/domain seat {target!r}; "
    "the tier never receives delegated dispatch authority" + _rule3e
)
EXPECTED_ERROR_SET["mastertier/CM226-pair-receipt-self-grant"] = [
    "05-receipt.md: commissioned pair receipt carries DELEGATED_DISPATCH_AUTHORITY: yes; "
    "self-grant is prohibited" + _commission_receipt_rule,
]
for _number, _target in (
    (227, "alpha.master-planner"), (228, "alpha.master-reviewer"),
    (229, "alpha.domain-planner"), (230, "alpha.domain-reviewer"),
):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-grant.md: " + _receiving.format(target=_target),
    ]

_rule3e_conflict = (
    "rule 3e gate: {field} carries 2 distinct values across 2 occurrences; "
    "a conflicting discriminator contributes no passing operand" + _h27_rule5_suffix
)
for _number in (231, 232):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-grant.md: " + _rule3e_conflict.format(field="DELEGATED_DISPATCH_AUTHORITY"),
    ]
for _number in (233, 234):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-grant.md: " + _rule3e_conflict.format(field="TO"),
    ]

_auth_ancestor = (
    "selected authorization-universe member 01-auth.md has conflicting COMMISSION_AUTHORIZATION occurrences; "
    "universe selection refuses first-value resolution" + _commission_auth_rule
)
_gate_files = ("02-charter.md", "03-approval.md", "04-grant.md", "05-receipt.md")
for _number, _last in enumerate(range(1, 5), start=235):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-auth.md: COMMISSION_AUTHORIZATION carries 2 distinct values across 2 occurrences; "
        "an authority-critical field is fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix,
        *[f"{_gate}: {_auth_ancestor}" for _gate in _gate_files[:_last]],
    ]

_charter_gate_labels = (
    ("03-approval.md", "charter revision"),
    ("04-grant.md", "latest charter revision"),
    ("05-receipt.md", "latest charter revision"),
)
for _number, _last in enumerate(range(1, 4), start=239):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "02-charter.md: " + _authority_conflict,
        *[
            f"{_gate}: selected {_label} 02-charter.md has conflicting AUTHORITY occurrences; "
            "ancestry resolution refuses first-value resolution" + (
                _commission_charter_rule if _gate.startswith("03-") else
                _commission_grant_rule if _gate.startswith("04-") else
                _commission_receipt_rule
            )
            for _gate, _label in _charter_gate_labels[:_last]
        ],
    ]

for _number, _last in ((242, 1), (243, 2)):
    _name = COMMISSION_MEMBERS[_number - 77]
    _errors = ["03-approval.md: " + _verdict_conflict]
    if _last >= 1:
        _errors.append(
            "04-grant.md: selected charter review 03-approval.md has conflicting "
            "DESIGN_REVIEW_VERDICT occurrences; ancestry resolution refuses first-value resolution"
            + _commission_grant_rule
        )
    if _last == 2:
        _errors.append(
            "05-receipt.md: selected charter review 03-approval.md has conflicting "
            "DESIGN_REVIEW_VERDICT occurrences; ancestry resolution refuses first-value resolution"
            + _commission_receipt_rule
        )
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = _errors
EXPECTED_ERROR_SET["mastertier/CM244-conflict-grant-at-receipt"] = [
    "04-grant.md: TO carries 2 distinct values across 2 occurrences; an authority-critical field is "
    "fail-closed unless exactly one distinct value is present" + _h27_rule5_suffix,
    "05-receipt.md: selected grant-universe member 04-grant.md has conflicting TO occurrences; "
    "universe selection refuses first-value resolution" + _commission_receipt_rule,
]

_grammar_error = (
    "commission machine engaged with non-grammatical COMMISSION_ID {value!r}; "
    "expected [a-z0-9][a-z0-9-]*" + _commission_rule
)
for _number, _value in ((253, "-a"), (254, "A1"), (255, "a-B"), (256, "a_b"), (257, "")):
    _name = COMMISSION_MEMBERS[_number - 77]
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-auth.md: " + _grammar_error.format(value=_value),
    ]
EXPECTED_ERROR_SET["mastertier/CM258-identity-wrong-ch-form"] = [
    "01-auth.md: CHARTER_DOC_ID 'charter-cm258' must equal 'CH-cm258'" + _commission_rule,
]
EXPECTED_ERROR_SET["mastertier/CM259-identity-charter-design-mismatch"] = [
    "02-charter.md: charter DESIGN_DOC_ID 'CH-other' must equal CHARTER_DOC_ID 'CH-cm259'"
    + _commission_charter_rule,
]
EXPECTED_ERROR_SET["mastertier/CM260-identity-composition-spelling"] = [
    "01-auth.md: CHARTER_DOC_ID 'CH-a-b' must equal 'CH-a--b'" + _commission_rule,
]
for _name in ("CM265-receiving-mixed-list", "CM266-receiving-identical-repeats"):
    EXPECTED_ERROR_SET[f"mastertier/{_name}"] = [
        "01-grant.md: " + _receiving.format(target="alpha.master-planner"),
    ]
EXPECTED_ERROR_SET.update({
    "mastertier/CM153-byte-exact-address-surface": [
        "02-charter.md: charter equality surface is not byte-equal to the selected authorization" + _commission_charter_rule,
    ],
    "mastertier/CM154-empty-carrier-engages": ["01-relay.md: " + _auth_shape],
    "mastertier/CM155-empty-carrier-engages": ["01-relay.md: " + _unconsumed],
    "mastertier/CM156-empty-carrier-engages": ["01-relay.md: " + _unconsumed],
    "mastertier/CM157-empty-carrier-engages": ["01-relay.md: " + _unconsumed],
    "mastertier/CM158-charter-equality-commission-id": [
        "02-charter.md: commission 'other' has no earlier authorization-universe member" + _commission_charter_rule,
    ],
    "mastertier/CM159-empty-commission-id-engages": ["01-relay.md: " + _unconsumed],
})
for _number, (_label, _target) in enumerate(
    (("operator-legacy", "operator.planner"), ("operator-explicit", "operator.pair-planner"),
     ("orchestrator-legacy", "orchestrator.planner"),
     ("orchestrator-explicit", "orchestrator.pair-planner")), start=164,
):
    EXPECTED_ERROR_SET[f"mastertier/CM{_number}-grant-target-{_label}"] = [
        f"04-grant.md: COMMISSION_TO {_target!r} is not exactly one non-special pair-planner address" + _commission_grant_rule,
    ]


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
