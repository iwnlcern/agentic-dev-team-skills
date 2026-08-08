#!/usr/bin/env python3
"""Generate the A5 enforcement-round fixture battery, rev32 semantics
plus a5-plan r4/r5 corrections (lockdigest/ + rootres/). Deterministic. Rerunnable."""
import hashlib, shutil
from pathlib import Path

FIX = Path(__file__).resolve().parent / "relay-lint-fixtures"
DR1 = FIX / "design-review/DR1-valid-design-doc-chain"

MEMBER = """ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: lockdigest-{id}
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: fixture.planner
TO: fixture.implementer
{fields}

Lock-digest fixture.

FINAL_GIT_STATUS_SHORT: none — fixture only
"""

GOOD = "a" * 64
ART = "fixture-plan"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


ART_TEXT = MEMBER.format(id="artifact", fields="IN_REPLY_TO: none") + "Artifact body.\n"
ARTBYTES = ART_TEXT.encode()
OTHERBYTES = (ART_TEXT + "divergent line\n").encode()
THIRDBYTES = (ART_TEXT + "third divergent line\n").encode()


def w(p: Path, content: str | bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content)
    else:
        p.write_bytes(content)


def member(fid, fields):
    return MEMBER.format(id=fid, fields=fields)


def build():
    if True:
        shutil.rmtree(FIX / "lockdigest", ignore_errors=True); shutil.rmtree(FIX / "rootres", ignore_errors=True)
    good_digest = sha(ARTBYTES)
    F = {}  # label -> (mode, target-rel, cwd-rel or None, seam-rel or None)

    def filecase(name, fields):
        w(FIX / "lockdigest" / f"{name}.md", member(name, fields))
        F[name] = ("file", f"lockdigest/{name}.md", None, None)

    D, A = "PLAN_SHA256", "PLAN_ARTIFACT"
    filecase("FLD01-valid-shape", f"{D}: {GOOD}\n{A}: {ART}")
    filecase("FLD04-digest-no-locator", f"{D}: {GOOD}")
    filecase("FLD05-digest-short", f"{D}: {'a'*63}\n{A}: {ART}")
    filecase("FLD06-digest-nonhex", f"{D}: {'g'*64}\n{A}: {ART}")
    filecase("FLD07-digest-upper", f"{D}: {'A'*64}\n{A}: {ART}")
    filecase("FLD08-stem-pathsep", f"{D}: {GOOD}\n{A}: plans/{ART}")
    filecase("FLD09-stem-atsign", f"{D}: {GOOD}\n{A}: {ART} @ sha256 {GOOD}")
    filecase("FLD10-stem-mdsuffix", f"{D}: {GOOD}\n{A}: {ART}.md")
    filecase("FLD11-badstem-no-digest", f"{A}: plans/{ART}")
    filecase("FLD12-locator-conflict-no-digest", f"{A}: {ART}\n{A}: other-stem")
    filecase("FLD13-identical-locator-repeat", f"{D}: {GOOD}\n{A}: {ART}\n{A}: {ART}")
    filecase("FLD14-identical-digest-repeat", f"{D}: {GOOD}\n{D}: {GOOD}\n{A}: {ART}")
    filecase("FLD15-digest-conflict", f"{D}: {'a'*64}\n{D}: {'b'*64}\n{A}: {ART}")
    filecase("FLD16-locator-conflict", f"{D}: {GOOD}\n{A}: {ART}\n{A}: other-stem")
    filecase("FLD17-locconflict-plus-baddigest", f"{D}: {'a'*63}\n{A}: {ART}\n{A}: other-stem")
    filecase("FLD18-missing-locator-baddigest", f"{D}: {'a'*63}")
    filecase("FLD19-both-conflict", f"{D}: {'a'*64}\n{D}: {'b'*64}\n{A}: {ART}\n{A}: other-stem")
    filecase("FLD20-badlocator-digestconflict", f"{D}: {'a'*64}\n{D}: {'b'*64}\n{A}: plans/{ART}")
    filecase("FLD21-digestconflict-all-malformed", f"{D}: {'a'*63}\n{D}: {'g'*64}\n{A}: {ART}")
    filecase("FLD22-locconflict-all-malformed", f"{D}: {GOOD}\n{A}: plans/{ART}\n{A}: {ART}.md")
    filecase("FLD23-missing-locator-digestconflict", f"{D}: {'a'*64}\n{D}: {'b'*64}")

    def rootcase(name, relroot, files, cwdrel=None, seam=None):
        for rel, content in files.items():
            w(FIX / name.split("|")[0] / rel if False else FIX / rel, content)
        F[name] = ("root", relroot, cwdrel, seam)

    gd = good_digest
    # RLD1 valid match at relay root
    w(FIX / "lockdigest/RLD1-valid-match/.relays/v29/01-plan-20260801-130000.md", member("rld1", f"{D}: {gd}\n{A}: {ART}"))
    w(FIX / "lockdigest/RLD1-valid-match/plans" / f"{ART}.md", ARTBYTES)
    F["RLD1-valid-match"] = ("root", "lockdigest/RLD1-valid-match/.relays/v29", None, None)
    # RLD2 production topology: DR1 chain + fields + artifacts
    dr1 = DR1
    dst = FIX / "lockdigest/RLD2-production-topology/.relays/v29"
    shutil.copytree(dr1, dst)
    for _i, _fn in enumerate(sorted(p.name for p in dst.glob("*.md")), 1):
        (dst / _fn).rename(dst / _fn.replace(".md", f"-20260801-1{_i}0000.md"))
    design_bytes = b"design artifact bytes\n"
    plan_text = (dst / "03-plan-20260801-130000.md").read_text()
    plan_text = plan_text.replace(
        "DESIGN_RECORD_KIND: design-doc",
        f"DESIGN_RECORD_KIND: design-doc\nDESIGN_SHA256: {sha(design_bytes)}\nDESIGN_ARTIFACT: fixture-design\n"
        f"PLAN_SHA256: {gd}\nPLAN_ARTIFACT: {ART}",
    )
    (dst / "03-plan-20260801-130000.md").write_text(plan_text)
    top = FIX / "lockdigest/RLD2-production-topology"
    w(top / "designs/fixture-design.md", design_bytes)
    w(top / "plans" / f"{ART}.md", ARTBYTES)
    F["RLD2-production-topology"] = ("root", "lockdigest/RLD2-production-topology/.relays/v29", None, None)
    # LDM1 missing artifact: file mode empty, root mode generic missing
    w(FIX / "lockdigest/LDM1-missing/01-plan-20260801-130000.md", member("ldm1", f"{D}: {gd}\n{A}: no-such-stem"))
    F["LDM1-missing-file-mode"] = ("file", "lockdigest/LDM1-missing/01-plan-20260801-130000.md", None, None)
    F["LDM1-missing-root-mode"] = ("root", "lockdigest/LDM1-missing", None, None)
    # LDM2 mismatch: file mode empty, root mode mismatch
    w(FIX / "lockdigest/LDM2-mismatch/.relays/v29/01-plan-20260801-130000.md", member("ldm2", f"{D}: {gd}\n{A}: {ART}"))
    w(FIX / "lockdigest/LDM2-mismatch/plans" / f"{ART}.md", OTHERBYTES)
    F["LDM2-mismatch-file-mode"] = ("file", "lockdigest/LDM2-mismatch/.relays/v29/01-plan-20260801-130000.md", None, None)
    (FIX / "lockdigest/LDM2-mismatch/cwd").mkdir(parents=True, exist_ok=True)
    F["LDM2-mismatch-root-mode"] = ("root", "lockdigest/LDM2-mismatch/.relays/v29", "lockdigest/LDM2-mismatch/cwd", None)
    # RLD5 directory candidate
    w(FIX / "lockdigest/RLD5-directory-candidate/01-plan-20260801-130000.md", member("rld5", f"{D}: {gd}\n{A}: {ART}"))
    (FIX / "lockdigest/RLD5-directory-candidate/plans" / f"{ART}.md").mkdir(parents=True)
    F["RLD5-directory-candidate"] = ("root", "lockdigest/RLD5-directory-candidate", None, None)
    # RLD9 seam alone
    w(FIX / "lockdigest/RLD9-read-seam/.relays/v29/01-plan-20260801-130000.md", member("rld9", f"{D}: {gd}\n{A}: {ART}"))
    w(FIX / "lockdigest/RLD9-read-seam/plans" / f"{ART}.md", ARTBYTES)
    (FIX / "lockdigest/RLD9-read-seam/cwd").mkdir(parents=True, exist_ok=True)
    F["RLD9-read-seam"] = ("root", "lockdigest/RLD9-read-seam/.relays/v29", "lockdigest/RLD9-read-seam/cwd", f"lockdigest/RLD9-read-seam/plans/{ART}.md")
    # RLD6 every-instance all match (root at .relays/v29; cwd probe via cwd/)
    base = FIX / "lockdigest/RLD6-every-instance-match"
    w(base / ".relays/v29/01-plan-20260801-130000.md", member("rld6", f"{D}: {gd}\n{A}: {ART}"))
    w(base / ".relays/v29/plans" / f"{ART}.md", ARTBYTES)
    w(base / "cwd/plans" / f"{ART}.md", ARTBYTES)
    w(base / "plans" / f"{ART}.md", ARTBYTES)
    F["RLD6-every-instance-match"] = ("root", "lockdigest/RLD6-every-instance-match/.relays/v29",
                                      "lockdigest/RLD6-every-instance-match/cwd", None)
    # RLD7 first match, BOTH later mismatch
    base = FIX / "lockdigest/RLD7-later-mismatch"
    w(base / ".relays/v29/01-plan-20260801-130000.md", member("rld7", f"{D}: {gd}\n{A}: {ART}"))
    w(base / ".relays/v29/plans" / f"{ART}.md", ARTBYTES)
    w(base / "cwd/plans" / f"{ART}.md", OTHERBYTES)
    w(base / "plans" / f"{ART}.md", THIRDBYTES)
    F["RLD7-later-mismatch"] = ("root", "lockdigest/RLD7-later-mismatch/.relays/v29",
                                "lockdigest/RLD7-later-mismatch/cwd", None)
    # RLD8 mixed partition: probe1 dir, probe2 seam, probe3 mismatch
    base = FIX / "lockdigest/RLD8-mixed-partition"
    w(base / ".relays/v29/01-plan-20260801-130000.md", member("rld8", f"{D}: {gd}\n{A}: {ART}"))
    (base / ".relays/v29/plans" / f"{ART}.md").mkdir(parents=True)
    w(base / "cwd/plans" / f"{ART}.md", ARTBYTES)
    w(base / "plans" / f"{ART}.md", OTHERBYTES)
    F["RLD8-mixed-partition"] = ("root", "lockdigest/RLD8-mixed-partition/.relays/v29",
                                 "lockdigest/RLD8-mixed-partition/cwd",
                                 f"lockdigest/RLD8-mixed-partition/cwd/plans/{ART}.md")

    IDXHDR = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n|---|---|---|---|---|---|---|---|---|---|\n"
    ROW = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | AUDIT-planner-20260801-130000.md |\n"

    def idxcase(name, index_text, files=(), cwdrel=None):
        d = FIX / "rootres" / name
        w(d / "INDEX.md", index_text)
        for rel in files:
            w(d / rel, "relay body\n")
        F[name] = ("index", f"rootres/{name}", cwdrel, None)

    idxcase("XR1-marked-resolving", IDXHDR + ROW + "root: .\n", ["AUDIT-planner-20260801-130000.md"])
    idxcase("XR2-marked-missing-file", IDXHDR + ROW + "root: .\n", cwdrel="rootres/XR2-marked-missing-file")
    idxcase("XR3-unmarked-unresolvable", IDXHDR + ROW)
    idxcase("XR4-duplicate-markers", IDXHDR + ROW + "root: .\nroot: .\n")
    idxcase("XR5-marker-nonexistent-dir", IDXHDR + ROW + "root: no-such-dir\n", ["AUDIT-planner-20260801-130000.md"])
    idxcase("XR6-marker-existing-file", IDXHDR + ROW + "root: AUDIT-planner-20260801-130000.md\n",
            ["AUDIT-planner-20260801-130000.md"])
    dirrow = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | somedir |\n"
    d = FIX / "rootres/XR7-cell-directory"
    w(d / "INDEX.md", IDXHDR + dirrow + "root: .\n")
    (d / "somedir").mkdir(parents=True, exist_ok=True)
    (d / "somedir/.keep").write_text("")
    F["XR7-cell-directory"] = ("index", "rootres/XR7-cell-directory", "rootres/XR7-cell-directory", None)
    decoys = ("```\nroot: decoy-fenced\n```\n"
              "`root: decoy-inline`\n"
              "> root: decoy-quoted\n"
              "the root: decoy-prose marker is described here\n")
    idxcase("XR8-decoys-one-live", IDXHDR + ROW + decoys + "root: .\n", ["AUDIT-planner-20260801-130000.md"])
    norelay = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | none — ack row |\n"
    idxcase("XR10-no-relay-cell", IDXHDR + norelay + "root: .\n")
    nearmiss = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | (no relay cut — D-1.4; acks) |\n"
    idxcase("XR11-near-miss-cell", IDXHDR + nearmiss + "root: .\n", cwdrel="rootres/XR11-near-miss-cell")
    # XR9 deployment: linter loc, cwd, INDEX loc, root all differ; decoys at cwd+tool
    base = FIX / "rootres/XR9-deployment"
    w(base / "idxhome/INDEX.md", IDXHDR + ROW + "root: ../real/relroot\n")
    w(base / "real/relroot/AUDIT-planner-20260801-130000.md", "relay body\n")
    (base / "cwd/relroot").mkdir(parents=True, exist_ok=True)
    (base / "cwd/relroot/.keep").write_text("")
    F["XR9-deployment"] = ("index", "rootres/XR9-deployment/idxhome", "rootres/XR9-deployment/cwd", None)
    return F




def fenced_member(fid, payload):
    """Member with the fence payload APPENDED after FINAL_GIT_STATUS_SHORT so
    neither sanitizer's stripping can hide the baseline-required trailer."""
    return member(fid, "IN_REPLY_TO: none") + "\nPayload:\n\n" + payload + "\n"

GOOD = "a" * 64
ART = "fixture-plan"
D, A = "PLAN_SHA256", "PLAN_ARTIFACT"


def build2():
    F = build()  # the rev60 battery, unchanged fixture bytes
    for _n in ("XR2-marked-missing-file", "XR7-cell-directory", "XR11-near-miss-cell"):
        _m, _r, _c, _s = F[_n]
        F[_n] = ("index-rel", _r, _c, _s)
    ART_A = ARTBYTES              # digest sha(ART_A) = "A" version
    ART_B = OTHERBYTES            # divergent
    shaA = sha(ART_A)
    shaB = sha(ART_B)

    def rly(fid, fields, stamp="20260801-130000", phase_prefix="01-plan"):
        return f"{phase_prefix}-{stamp}.md", member(fid, fields)

    def nest(name, files, art=None, artname=None):
        base = FIX / "lockdigest" / name
        for fn, content in files:
            w(base / ".relays/v29" / fn, content)
        if art is not None:
            w(base / "plans" / f"{artname or ART}.md", art)
        return f"lockdigest/{name}/.relays/v29"

    # AH1 float-forward: early pins A, later pins B, bytes B -> empty
    root = nest("AH1-float-forward", [
        rly("ah1e", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        rly("ah1l", f"{D}: {shaB}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_B)
    F["AH1-float-forward"] = ("root", root, None, None)
    # AH2 rollback: same relays, bytes A -> mismatch on later only
    root = nest("AH2-rollback", [
        rly("ah2e", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        rly("ah2l", f"{D}: {shaB}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_A)
    F["AH2-rollback"] = ("root", root, None, None)
    # AH3a later MALFORMED carrier: earlier valid A governs, bytes B
    root = nest("AH3a-later-malformed", [
        rly("ah3ae", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        rly("ah3al", f"{D}: {'a'*63}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_B)
    F["AH3a-later-malformed"] = ("root", root, None, None)
    # AH3b later CONFLICTED carrier
    root = nest("AH3b-later-conflicted", [
        rly("ah3be", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        rly("ah3bl", f"{D}: {'a'*64}\n{D}: {'b'*64}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_B)
    F["AH3b-later-conflicted"] = ("root", root, None, None)
    # AH4 grouping independence: two stems, interleaved
    base = FIX / "lockdigest/AH4-two-groups"
    w(base / ".relays/v29/01-plan-20260801-100000.md", member("ah4a", f"{D}: {shaA}\n{A}: {ART}"))
    w(base / ".relays/v29/02-plan-20260801-130000.md", member("ah4b", f"{D}: {shaB}\n{A}: {ART}"))
    w(base / ".relays/v29/03-plan-20260801-110000.md", member("ah4c", f"{D}: {shaA}\n{A}: other-plan"))
    w(base / "plans" / f"{ART}.md", ART_B)          # group 1: later B governs, bytes B -> empty
    w(base / "plans/other-plan.md", ART_B)           # group 2: A governs, bytes B -> mismatch on 03
    F["AH4-two-groups"] = ("root", "lockdigest/AH4-two-groups/.relays/v29", None, None)
    # AH5 same-basename cross-directory same-stamp: distinct + identical twins
    base = FIX / "lockdigest/AH5a-samebase-distinct"
    w(base / ".relays/v29/x/01-plan-20260801-130000.md", member("ah5x", f"{D}: {shaA}\n{A}: {ART}"))
    w(base / ".relays/v29/y/01-plan-20260801-130000.md", member("ah5y", f"{D}: {shaB}\n{A}: {ART}"))
    w(base / "plans" / f"{ART}.md", ART_A)
    F["AH5a-samebase-distinct"] = ("root", "lockdigest/AH5a-samebase-distinct/.relays/v29", None, None)
    base = FIX / "lockdigest/AH5b-samebase-identical"
    w(base / ".relays/v29/x/01-plan-20260801-130000.md", member("ah5bx", f"{D}: {shaA}\n{A}: {ART}"))
    w(base / ".relays/v29/y/01-plan-20260801-130000.md", member("ah5by", f"{D}: {shaA}\n{A}: {ART}"))
    w(base / "plans" / f"{ART}.md", ART_A)
    F["AH5b-samebase-identical"] = ("root", "lockdigest/AH5b-samebase-identical/.relays/v29", None, None)
    # AH6 different-basename same-stamp: distinct + identical twins
    root = nest("AH6a-diffbase-distinct", [
        rly("ah6x", f"{D}: {shaA}\n{A}: {ART}", "20260801-130000", "01-design"),
        rly("ah6y", f"{D}: {shaB}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_A)
    F["AH6a-diffbase-distinct"] = ("root", root, None, None)
    root = nest("AH6b-diffbase-identical", [
        rly("ah6bx", f"{D}: {shaA}\n{A}: {ART}", "20260801-130000", "01-design"),
        rly("ah6by", f"{D}: {shaA}\n{A}: {ART}", "20260801-130000", "02-plan"),
    ], art=ART_A)
    F["AH6b-diffbase-identical"] = ("root", root, None, None)
    # STAMP: later impossible-calendar carrier ineligible; earlier valid governs
    root = nest("STAMP-impossible-later", [
        rly("stampe", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        rly("stampl", f"{D}: {shaB}\n{A}: {ART}", "20260231-120000", "02-plan"),
    ], art=ART_A)
    F["STAMP-impossible-later"] = ("root", root, None, None)
    # Template-mode rows
    w(FIX / "lockdigest/TM1-placeholder.md", member("tm1", f"{D}: <sha256-of-plan>\n{A}: <plan-stem>"))
    F["TM1-placeholder"] = ("template", "lockdigest/TM1-placeholder.md", None, None)
    w(FIX / "lockdigest/TM2-ordinary-malformed.md", member("tm2", f"{D}: {'a'*63}\n{A}: plans/{ART}"))
    F["TM2-ordinary-malformed"] = ("template", "lockdigest/TM2-ordinary-malformed.md", None, None)
    w(FIX / "lockdigest/TM3-conflicting-duplicates.md", member("tm3", f"{D}: {'a'*64}\n{D}: {'b'*64}\n{A}: {ART}"))
    F["TM3-conflicting-duplicates"] = ("template", "lockdigest/TM3-conflicting-duplicates.md", None, None)
    # Spaced-path marker
    dd = FIX / "rootres/XR12-spaced-root"
    hdr = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n|---|---|---|---|---|---|---|---|---|---|\n"
    row = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | AUDIT-planner-20260801-130000.md |\n"
    w(dd / "INDEX.md", hdr + row + "root: real dir\n")
    w(dd / "real dir/AUDIT-planner-20260801-130000.md", "relay body\n")
    F["XR12-spaced-root"] = ("index", "rootres/XR12-spaced-root", None, None)
    # r2 R1: non-hyphen accepted stamp governs; unstamped control
    root = nest("STAMP2-nonhyphen-governs", [
        rly("st2e", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        ("02-plan-20260801T140000Z.md", member("st2l", f"{D}: {shaB}\n{A}: {ART}")),
    ], art=ART_B)
    F["STAMP2-nonhyphen-governs"] = ("root", root, None, None)
    root = nest("UNSTAMPED-ineligible-control", [
        ("01-plan.md", member("unst", f"{D}: {shaB}\n{A}: {ART}")),
    ], art=ART_A)
    F["UNSTAMPED-ineligible-control"] = ("root", root, None, None)
    # r2 R2: identical-value co-governor MISMATCH attribution
    base = FIX / "lockdigest/AH5c-cogovernor-mismatch"
    w(base / ".relays/v29/x/01-plan-20260801-130000.md", member("ah5cx", f"{D}: {shaB}\n{A}: {ART}"))
    w(base / ".relays/v29/y/01-plan-20260801-130000.md", member("ah5cy", f"{D}: {shaB}\n{A}: {ART}"))
    w(base / "plans" / f"{ART}.md", ART_A)
    (base / "cwd").mkdir(parents=True, exist_ok=True)
    F["AH5c-cogovernor-mismatch"] = ("root", "lockdigest/AH5c-cogovernor-mismatch/.relays/v29",
                                     "lockdigest/AH5c-cogovernor-mismatch/cwd", None)
    # hardening: three-carrier A,A,B — {n} counts distinct VALUES
    root = nest("AH7-three-carrier-AAB", [
        ("01-plan-20260801-130000.md", member("ah7a", f"{D}: {shaA}\n{A}: {ART}")),
        ("02-plan-20260801-130000.md", member("ah7b", f"{D}: {shaA}\n{A}: {ART}")),
        ("03-plan-20260801-130000.md", member("ah7c", f"{D}: {shaB}\n{A}: {ART}")),
    ], art=ART_A)
    F["AH7-three-carrier-AAB"] = ("root", root, None, None)
    # r2 R3: no-data-row marker validation (relative-target registrations)
    hdr2 = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n|---|---|---|---|---|---|---|---|---|---|\n"
    dd = FIX / "rootres/XR13-norows-duplicate-markers"
    w(dd / "INDEX.md", hdr2 + "root: .\nroot: .\n")
    F["XR13-norows-duplicate-markers"] = ("index-rel", "rootres/XR13-norows-duplicate-markers", "rootres/XR13-norows-duplicate-markers", None)
    dd = FIX / "rootres/XR14-norows-bad-root"
    w(dd / "INDEX.md", hdr2 + "root: no-such-dir\n")
    F["XR14-norows-bad-root"] = ("index-rel", "rootres/XR14-norows-bad-root", "rootres/XR14-norows-bad-root", None)
    # rev32 fence battery (amend-6): TG/TF/MF twins + FB1-FB6
    shaC = sha(b"third body\n")
    # TG1: tilde-fenced later declaration would otherwise govern -> inert, earlier governs, bytes A -> EMPTY
    root = nest("TG1-tilde-governance", [
        rly("tg1e", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        ("02-plan-20260801-130000.md", fenced_member("tg1l", f"~~~\n{D}: {shaB}\n{A}: {ART}\n~~~")),
    ], art=ART_A)
    F["TG1-tilde-governance"] = ("root", root, None, None)
    # TF1: file-mode tilde-fenced malformed pair -> EMPTY
    w(FIX / "lockdigest/TF1-tilde-file-shape.md", fenced_member("tf1", f"~~~\n{D}: {'a'*63}\n{A}: plans/{ART}\n~~~"))
    F["TF1-tilde-file-shape"] = ("file", "lockdigest/TF1-tilde-file-shape.md", None, None)
    # MFa: tilde region containing backtick fence lines stays one region -> EMPTY
    w(FIX / "lockdigest/MFa-tilde-holds-backtick.md", fenced_member("mfa", f"~~~\n```\n{D}: {'a'*63}\n{A}: plans/{ART}\n```\n~~~"))
    F["MFa-tilde-holds-backtick"] = ("file", "lockdigest/MFa-tilde-holds-backtick.md", None, None)
    # MFb: backtick region containing tilde fence lines stays one region -> EMPTY
    w(FIX / "lockdigest/MFb-backtick-holds-tilde.md", fenced_member("mfb", f"```\n~~~\n{D}: {'a'*63}\n{A}: plans/{ART}\n~~~\n```"))
    F["MFb-backtick-holds-tilde"] = ("file", "lockdigest/MFb-backtick-holds-tilde.md", None, None)
    # FB1: ~~~~ opener, bare ~~~ inside (inert decl), ~~~~~ longer closer, decl after OPERATIONAL (mismatch)
    root = nest("FB1-longer-closer", [
        ("01-plan-20260801-130000.md", fenced_member("fb1",
            f"~~~~\n{D}: {shaB}\n{A}: {ART}\n~~~\n{D}: {shaC}\n{A}: {ART}\n~~~~~\n{D}: {shaB}\n{A}: {ART}")),
    ], art=ART_A)
    F["FB1-longer-closer"] = ("root", root, None, None)
    # FB2: 4-space-indented fence-like line is content; following malformed pair OPERATIONAL (file mode)
    w(FIX / "lockdigest/FB2-indented-nonopener.md", fenced_member("fb2", f"    ```\n{D}: {'a'*63}\n{A}: plans/{ART}"))
    F["FB2-indented-nonopener"] = ("file", "lockdigest/FB2-indented-nonopener.md", None, None)
    # FB3: suffix-bearing pseudo-closer does not close -> both decls inside -> EMPTY (file mode)
    w(FIX / "lockdigest/FB3-pseudo-closer-suffix.md", fenced_member("fb3", f"```\n{D}: {'a'*63}\n```extra\n{D}: {'b'*63}\n{A}: plans/{ART}\n```"))
    F["FB3-pseudo-closer-suffix"] = ("file", "lockdigest/FB3-pseudo-closer-suffix.md", None, None)
    # FB4: unmatched opener strips to EOF -> EMPTY (file mode)
    w(FIX / "lockdigest/FB4-unclosed-eof.md", fenced_member("fb4", f"```\n{D}: {'a'*63}\n{A}: plans/{ART}"))
    F["FB4-unclosed-eof"] = ("file", "lockdigest/FB4-unclosed-eof.md", None, None)
    # FB5: tilde composite at the exact 3-space boundary (root; earlier relay pins A, bytes A)
    root = nest("FB5-three-space-composite", [
        rly("fb5e", f"{D}: {shaA}\n{A}: {ART}", "20260801-100000"),
        ("02-plan-20260801-130000.md", fenced_member("fb5l",
            "   ~~~ info`~\n" + f"{D}: {shaB}\n{A}: {ART}\n" + "    ~~~~\n" + f"{D}: {shaC}\n{A}: {ART}\n" + "   ~~~~\n" + f"{D}: {shaB}\n{A}: {ART}")),
    ], art=ART_A)
    F["FB5-three-space-composite"] = ("root", root, None, None)
    # FB6a: legal backtick info-string opener opens -> enclosed malformed pair inert -> EMPTY (file)
    w(FIX / "lockdigest/FB6a-info-opener.md", fenced_member("fb6a", f"```python\n{D}: {'a'*63}\n{A}: plans/{ART}\n```"))
    F["FB6a-info-opener"] = ("file", "lockdigest/FB6a-info-opener.md", None, None)
    # FB6b: backtick-in-info line does NOT open -> following malformed pair OPERATIONAL (file)
    w(FIX / "lockdigest/FB6b-backtick-info-nonopener.md", fenced_member("fb6b", "```py`x\n" + f"{D}: {'a'*63}\n{A}: plans/{ART}"))
    F["FB6b-backtick-info-nonopener"] = ("file", "lockdigest/FB6b-backtick-info-nonopener.md", None, None)
    # FB7: U+00A0-suffixed same-family line is CONTENT, not a closer (r4 R2)
    w(FIX / "lockdigest/FB7-nbsp-pseudo-closer.md", fenced_member("fb7",
        "```\n" + f"{D}: {'a'*63}\n" + "```\u00a0\n" + f"{D}: {'b'*63}\n{A}: plans/{ART}\n" + "```"))
    F["FB7-nbsp-pseudo-closer"] = ("file", "lockdigest/FB7-nbsp-pseudo-closer.md", None, None)
    # r5 R3 discriminators: U+2028 must never create a line boundary
    fb8_payload = "```\n" + f"{D}: {'a'*63}\n" + "```\u2028\n" + f"{D}: {'b'*63}\n{A}: plans/{ART}\n" + "```"
    w(FIX / "lockdigest/FB8-u2028-pseudo-closer.md", fenced_member("fb8", fb8_payload))
    F["FB8-u2028-pseudo-closer"] = ("file", "lockdigest/FB8-u2028-pseudo-closer.md", None, None)
    w(FIX / "lockdigest/FLD24-u2028-field-decoy.md", member("fld24", "prose before\u2028" + f"{D}: {'a'*63}"))
    F["FLD24-u2028-field-decoy"] = ("file", "lockdigest/FLD24-u2028-field-decoy.md", None, None)
    hdr16 = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n|---|---|---|---|---|---|---|---|---|---|\n"
    row16 = "| 20260801-130000 | AUDIT | Planner | x1 | — | qi.planner | qi.implementer | — | returned | no-such-file.md |\n"
    dd = FIX / "rootres/XR16-u2028-marker-decoy"
    w(dd / "INDEX.md", hdr16 + row16 + "prose line\u2028root: .\n")
    F["XR16-u2028-marker-decoy"] = ("index", "rootres/XR16-u2028-marker-decoy", None, None)
    # XR8 gains the tilde-fenced marker decoy (fixture bytes change, expected set unchanged)
    xr8 = FIX / "rootres/XR8-decoys-one-live/INDEX.md"
    _old8 = "```" + "\n" + "root: decoy-fenced" + "\n" + "```" + "\n"
    _new8 = _old8 + "~~~" + "\n" + "root: decoy-tilde" + "\n" + "~~~" + "\n"
    _t8 = xr8.read_text()
    assert _t8.count(_old8) == 1
    xr8.write_text(_t8.replace(_old8, _new8))
    return F




NEUTRAL_CWDS = [
    "lockdigest/AH5c-cogovernor-mismatch/cwd",
    "lockdigest/AH1-float-forward/cwd", "lockdigest/AH2-rollback/cwd",
    "lockdigest/AH3a-later-malformed/cwd", "lockdigest/AH3b-later-conflicted/cwd",
    "lockdigest/AH4-two-groups/cwd", "lockdigest/STAMP-impossible-later/cwd",
    "lockdigest/FB1-longer-closer/cwd", "lockdigest/FB5-three-space-composite/cwd",
    "lockdigest/TG1-tilde-governance/cwd",
]
KEEPS = [
    "lockdigest/RLD5-directory-candidate/plans/fixture-plan.md/.keep",
    "lockdigest/RLD8-mixed-partition/.relays/v29/plans/fixture-plan.md/.keep",
    "lockdigest/LDM2-mismatch/cwd/.keep",
    "lockdigest/RLD9-read-seam/cwd/.keep",
] + [c + "/.keep" for c in NEUTRAL_CWDS]

if __name__ == "__main__":
    build2()
    for k in KEEPS:
        p = FIX / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    print("A5 fixtures generated")
