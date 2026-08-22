#!/usr/bin/env python3
"""Materialize deterministic Git-backed cross-root relay-lint fixtures."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import TypeAlias

Case: TypeAlias = tuple[str, Path, int, list[str] | None]

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-08-17T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-08-17T00:00:00 +0000",
    "HOME": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

DESIGN = """ROLE: Domain Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: xroot-gov-design-1
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no — fixture
FROM: m-1.domain-planner
TO: m-1.domain-reviewer
DESIGN_DOC_ID: d1
DESIGN_RECORD_KIND: design-doc
FINAL_GIT_STATUS_SHORT: none — fixture
"""

REVIEW = """ROLE: Domain Reviewer
PHASE: DESIGN-REVIEW
AUTHORITY: review-only
DISPATCH_ID: xroot-gov-review-1
PARENT_DISPATCH_ID: xroot-gov-design-1
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no — fixture
FROM: m-1.domain-reviewer
TO: m-1.domain-planner
DESIGN_DOC_ID: d1
DESIGN_RECORD_KIND: design-doc
DESIGN_REVIEW_VERDICT: approve
FINAL_GIT_STATUS_SHORT: none — fixture
"""

ARTIFACT = "# Fixture design d1\n\nPinned cross-root design bytes.\n"
PLAN_NAME = "PLAN-pair-planner-20260817-120000.md"
ORIGIN_NAME = "DESIGN-planner-20260801-100000.md"
REVIEW_NAME = "DESIGN-REVIEW-reviewer-20260801-110000.md"
XROOT_PREFIX = "declared design edge failed verification"


def _git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    env = os.environ.copy()
    env.update(GIT_ENV)
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
    )
    return completed.stdout.strip() if text else completed.stdout


def _literal_object(repo: Path, kind: str, content: bytes) -> str:
    raw = f"{kind} {len(content)}\0".encode("ascii") + content
    oid = hashlib.sha1(raw).hexdigest()
    object_path = repo / ".git" / "objects" / oid[:2] / oid[2:]
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(zlib.compress(raw))
    return oid


def _tree_entry(mode: bytes, name: bytes, oid: str) -> bytes:
    return mode + b" " + name + b"\x00" + bytes.fromhex(oid)


def _literal_commit(repo: Path, tree_oid: str) -> str:
    content = (
        f"tree {tree_oid}\n"
        "author fixture <fixture@example.invalid> 1786924800 +0000\n"
        "committer fixture <fixture@example.invalid> 1786924800 +0000\n"
        "\nfixture: hostile object graph\n"
    ).encode("ascii")
    return _literal_object(repo, "commit", content)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **GIT_ENV},
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _foreign_repo(case_root: Path, mutate_worktree=None) -> tuple[Path, str, str]:
    repo = case_root / "pdc"
    _init_repo(repo)
    _write(repo / "relays" / ORIGIN_NAME, DESIGN)
    _write(repo / "relays" / REVIEW_NAME, REVIEW)
    _write(repo / "designs" / "d1.md", ARTIFACT)
    if mutate_worktree is not None:
        mutate_worktree(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture: xroot authority chain")
    commit = str(_git(repo, "rev-parse", "HEAD"))
    digest = hashlib.sha256(ARTIFACT.encode("utf-8")).hexdigest()
    return repo, commit, digest


def _plan_fields(repo: Path, commit: str, digest: str) -> list[tuple[str, str]]:
    return [
        ("ROLE", "Pair Planner"),
        ("PHASE", "PLAN"),
        ("AUTHORITY", "plan-only"),
        ("DISPATCH_ID", "xroot-consume-plan-1"),
        ("CEREMONY_TIER", "medium"),
        ("EVIDENCE_TARGET", "E2"),
        ("HUMAN_GATE_REQUIRED", "no — fixture"),
        ("FROM", "s4.pair-planner"),
        ("TO", "s4.pair-implementer"),
        ("DESIGN_LOCK_ID", f"d1-{digest[:8]}-lock-20260817"),
        ("DESIGN_RECORD_KIND", "design-doc"),
        ("DESIGN_DOC_ID", "d1"),
        ("DESIGN_OWNER", "m-1"),
        ("DESIGN_SOURCE_REPO", str(repo)),
        ("DESIGN_SOURCE_COMMIT", commit),
        ("DESIGN_SOURCE_ROOT", "relays"),
        ("DESIGN_SOURCE_PATH", "designs/d1.md"),
        ("DESIGN_SHA256", digest),
        ("FINAL_GIT_STATUS_SHORT", "none — fixture"),
    ]


def _render(fields: list[tuple[str, str]]) -> str:
    return "".join(f"{key}: {value}\n" for key, value in fields)


def _replace(fields: list[tuple[str, str]], key: str, value: str) -> None:
    for index, (candidate, _old) in enumerate(fields):
        if candidate == key:
            fields[index] = (key, value)
            return
    raise KeyError(key)


def _remove(fields: list[tuple[str, str]], key: str) -> None:
    fields[:] = [(candidate, value) for candidate, value in fields if candidate != key]


def _xerror(axis: str, detail: str) -> str:
    return f"{PLAN_NAME}: {XROOT_PREFIX}: {axis}: {detail}"


def _build_case(
    dest: Path,
    name: str,
    *,
    mutate_plan=None,
    mutate_foreign=None,
    prepare=None,
    local_relays: dict[str, str] | None = None,
    expected_exit: int = 0,
    expected_errors: list[str] | None = None,
) -> Case:
    case_root = dest / name
    foreign, commit, digest = _foreign_repo(case_root, mutate_foreign)
    consuming = case_root / "build"
    _init_repo(consuming)
    relay_root = consuming / ".relays" / "x"
    fields = _plan_fields(foreign, commit, digest)
    dispatch_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    _replace(fields, "DISPATCH_ID", f"xroot-{dispatch_slug}")
    if mutate_plan is not None:
        mutate_plan(fields)
    if prepare is not None:
        prepare(case_root, foreign, fields)
    _write(relay_root / PLAN_NAME, _render(fields))
    for filename, text in (local_relays or {}).items():
        _write(relay_root / filename, text)
    return (f"xroot/{name}", relay_root, expected_exit, expected_errors)


def _local_chain() -> dict[str, str]:
    design = DESIGN.replace("Domain Planner", "Pair Planner").replace(
        "m-1.domain-planner", "s4.pair-planner"
    ).replace("m-1.domain-reviewer", "s4.pair-implementer")
    review = REVIEW.replace("Domain Reviewer", "Pair Implementer").replace(
        "m-1.domain-reviewer", "s4.pair-implementer"
    ).replace("m-1.domain-planner", "s4.pair-planner")
    return {
        "DESIGN-pair-planner-20260817-100000.md": design,
        "DESIGN-REVIEW-pair-implementer-20260817-110000.md": review,
    }


def _unengaged_plan(fields: list[tuple[str, str]]) -> None:
    for key in (
        "DESIGN_OWNER",
        "DESIGN_SOURCE_REPO",
        "DESIGN_SOURCE_COMMIT",
        "DESIGN_SOURCE_ROOT",
        "DESIGN_SOURCE_PATH",
        "DESIGN_SHA256",
    ):
        _remove(fields, key)
    _replace(fields, "DESIGN_LOCK_ID", "d1")
    fields.insert(-1, ("PARENT_DISPATCH_ID", "xroot-gov-review-1"))


def _digest_control(fields: list[tuple[str, str]]) -> None:
    for key in (
        "DESIGN_LOCK_ID",
        "DESIGN_RECORD_KIND",
        "DESIGN_DOC_ID",
        "DESIGN_OWNER",
        "DESIGN_SOURCE_REPO",
        "DESIGN_SOURCE_COMMIT",
        "DESIGN_SOURCE_ROOT",
        "DESIGN_SOURCE_PATH",
    ):
        _remove(fields, key)


def _wrong_phase_boundary(fields: list[tuple[str, str]]) -> None:
    _digest_control(fields)
    _replace(fields, "ROLE", "Domain Planner")
    _replace(fields, "PHASE", "DESIGN")
    _replace(fields, "AUTHORITY", "design-only")
    _replace(fields, "FROM", "m-1.domain-planner")
    _replace(fields, "TO", "m-1.domain-reviewer")
    fields.insert(-2, ("DESIGN_SOURCE_PATH", "designs/d1.md"))


def _absent_repo(case_root: Path, _foreign: Path, fields: list[tuple[str, str]]) -> None:
    _replace(fields, "DESIGN_SOURCE_REPO", str(case_root / "absent"))


def _not_repo(case_root: Path, _foreign: Path, fields: list[tuple[str, str]]) -> None:
    target = case_root / "not-a-repo"
    target.mkdir()
    _replace(fields, "DESIGN_SOURCE_REPO", str(target))


def _relative_repo(_case_root: Path, _foreign: Path, fields: list[tuple[str, str]]) -> None:
    _replace(fields, "DESIGN_SOURCE_REPO", "../pdc")


def _repo_subdirectory(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    _replace(fields, "DESIGN_SOURCE_REPO", str(foreign / "designs"))


def _tag_commit(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    _git(foreign, "tag", "-a", "fixture-tag", "-m", "fixture annotated tag")
    _replace(fields, "DESIGN_SOURCE_COMMIT", str(_git(foreign, "rev-parse", "fixture-tag")))


def _symlink_component(repo: Path) -> None:
    (repo / "links").symlink_to("designs", target_is_directory=True)


def _quoted_utf8_component(repo: Path) -> None:
    _write(repo / 'desi"gn-é' / "d1.md", ARTIFACT)


def _second_commit_digest(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    changed = ARTIFACT + "Second revision.\n"
    _write(foreign / "designs" / "d1.md", changed)
    _git(foreign, "add", "designs/d1.md")
    _git(foreign, "commit", "-q", "-m", "fixture: later artifact bytes")
    _replace(fields, "DESIGN_SHA256", hashlib.sha256(changed.encode("utf-8")).hexdigest())


def _fifo_object(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    oid = "a" * 40
    object_path = foreign / ".git" / "objects" / oid[:2] / oid[2:]
    object_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(object_path)
    _replace(fields, "DESIGN_SOURCE_COMMIT", oid)


def _non_ascii_tree_mode(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    child_oid = str(_git(foreign, "rev-parse", "HEAD:relays"))
    malformed_tree = _literal_object(
        foreign,
        "tree",
        _tree_entry(b"\xff00000", b"relays", child_oid),
    )
    _replace(fields, "DESIGN_SOURCE_COMMIT", _literal_commit(foreign, malformed_tree))


def _deep_tree(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
    tree_oid = _literal_object(foreign, "tree", b"")
    for _ in range(2500):
        tree_oid = _literal_object(
            foreign,
            "tree",
            _tree_entry(b"40000", b"d", tree_oid),
        )
    designs_oid = str(_git(foreign, "rev-parse", "HEAD:designs"))
    root_tree = _literal_object(
        foreign,
        "tree",
        _tree_entry(b"40000", b"designs", designs_oid)
        + _tree_entry(b"40000", b"relays", tree_oid),
    )
    _replace(fields, "DESIGN_SOURCE_COMMIT", _literal_commit(foreign, root_tree))


def _relay(repo: Path, name: str) -> Path:
    return repo / "relays" / name


def _replace_relay_field(repo: Path, name: str, key: str, value: str) -> None:
    path = _relay(repo, name)
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[index] = f"{key}: {value}"
            _write(path, "\n".join(lines) + "\n")
            return
    raise KeyError((name, key))


def _remove_relay_field(repo: Path, name: str, key: str) -> None:
    path = _relay(repo, name)
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{key}:")
    ]
    _write(path, "\n".join(lines) + "\n")


def _add_relay_field(repo: Path, name: str, key: str, value: str, *, first: bool) -> None:
    path = _relay(repo, name)
    lines = path.read_text(encoding="utf-8").splitlines()
    if first:
        lines.insert(0, f"{key}: {value}")
    else:
        lines.append(f"{key}: {value}")
    _write(path, "\n".join(lines) + "\n")


def _copy_relay(repo: Path, source: str, target: str) -> None:
    _write(_relay(repo, target), _relay(repo, source).read_text(encoding="utf-8"))


def _latest_review(repo: Path, mutate) -> None:
    name = "DESIGN-REVIEW-reviewer-20260801-120000.md"
    _copy_relay(repo, REVIEW_NAME, name)
    _replace_relay_field(repo, name, "DISPATCH_ID", "xroot-gov-review-2")
    mutate(repo, name)


def _latest_origin(repo: Path, mutate) -> None:
    name = "DESIGN-planner-20260801-105000.md"
    _copy_relay(repo, ORIGIN_NAME, name)
    _replace_relay_field(repo, name, "DISPATCH_ID", "xroot-gov-design-2")
    mutate(repo, name)


def _authority_cases(dest: Path) -> list[Case]:
    cases: list[Case] = []

    def add(name: str, mutate_foreign=None, mutate_plan=None, axis="authority-population", detail=""):
        cases.append(_build_case(
            dest,
            name,
            mutate_foreign=mutate_foreign,
            mutate_plan=mutate_plan,
            expected_exit=1,
            expected_errors=[_xerror(axis, detail)],
        ))

    add(
        "X21-no-origin",
        lambda repo: _relay(repo, ORIGIN_NAME).unlink(),
        detail="no DESIGN origin found for DESIGN_DOC_ID d1",
    )

    def wrong_doc(repo: Path) -> None:
        _replace_relay_field(repo, ORIGIN_NAME, "DESIGN_DOC_ID", "d2")
        _replace_relay_field(repo, REVIEW_NAME, "DESIGN_DOC_ID", "d2")

    add(
        "X21b-wrong-doc-identity",
        wrong_doc,
        detail="no DESIGN origin found for DESIGN_DOC_ID d1",
    )
    add(
        "X22-owner-mismatch",
        mutate_plan=lambda fields: _replace(fields, "DESIGN_OWNER", "m-2"),
        axis="authority-owner",
        detail="selected origin owner m-1 does not match DESIGN_OWNER m-2",
    )

    def owner_two(repo: Path) -> None:
        for name in (ORIGIN_NAME, REVIEW_NAME):
            text = _relay(repo, name).read_text(encoding="utf-8").replace("m-1.", "m-2.")
            _write(_relay(repo, name), text)

    cases.append(_build_case(
        dest,
        "X22b-owner-match-control",
        mutate_foreign=owner_two,
        mutate_plan=lambda fields: _replace(fields, "DESIGN_OWNER", "m-2"),
        expected_errors=[],
    ))
    add(
        "X23-review-missing",
        lambda repo: _relay(repo, REVIEW_NAME).unlink(),
        axis="authority-parent",
        detail="no DESIGN-REVIEW found for selected origin xroot-gov-design-1",
    )
    add(
        "X24-verdict-must-revise",
        lambda repo: _replace_relay_field(repo, REVIEW_NAME, "DESIGN_REVIEW_VERDICT", "must-revise"),
        axis="authority-verdict",
        detail=f"selected review {REVIEW_NAME} verdict must-revise is not approve",
    )
    add(
        "X25-stale-approval",
        lambda repo: _latest_review(repo, lambda r, n: _replace_relay_field(r, n, "DESIGN_REVIEW_VERDICT", "must-revise")),
        axis="authority-verdict",
        detail="selected review DESIGN-REVIEW-reviewer-20260801-120000.md verdict must-revise is not approve",
    )

    review_matrix = [
        ("absent-kind", lambda r, n: _remove_relay_field(r, n, "DESIGN_RECORD_KIND"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md lacks DESIGN_RECORD_KIND"),
        ("wrong-kind", lambda r, n: _replace_relay_field(r, n, "DESIGN_RECORD_KIND", "audit-record"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md DESIGN_RECORD_KIND is not design-doc"),
        ("absent-role", lambda r, n: _remove_relay_field(r, n, "ROLE"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md lacks ROLE"),
        ("absent-dispatch", lambda r, n: _remove_relay_field(r, n, "DISPATCH_ID"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md lacks DISPATCH_ID"),
        ("wrong-authority", lambda r, n: _replace_relay_field(r, n, "AUTHORITY", "plan-only"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md AUTHORITY is not review-only"),
        ("role-from-mismatch", lambda r, n: _replace_relay_field(r, n, "ROLE", "Pair Implementer"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md ROLE does not match FROM"),
        ("bad-verdict", lambda r, n: _replace_relay_field(r, n, "DESIGN_REVIEW_VERDICT", "bogus"), "selected review DESIGN-REVIEW-reviewer-20260801-120000.md has invalid DESIGN_REVIEW_VERDICT"),
    ]
    for suffix, mutation, detail in review_matrix:
        add(
            f"X26-newer-malformed-review-{suffix}",
            lambda repo, mutation=mutation: _latest_review(repo, mutation),
            detail=detail,
        )

    origin_matrix = [
        ("absent-kind", lambda r, n: _remove_relay_field(r, n, "DESIGN_RECORD_KIND"), "selected origin DESIGN-planner-20260801-105000.md lacks DESIGN_RECORD_KIND"),
        ("wrong-kind", lambda r, n: _replace_relay_field(r, n, "DESIGN_RECORD_KIND", "audit-record"), "selected origin DESIGN-planner-20260801-105000.md DESIGN_RECORD_KIND is not design-doc"),
        ("absent-role", lambda r, n: _remove_relay_field(r, n, "ROLE"), "selected origin DESIGN-planner-20260801-105000.md lacks ROLE"),
        ("absent-dispatch", lambda r, n: _remove_relay_field(r, n, "DISPATCH_ID"), "selected origin DESIGN-planner-20260801-105000.md lacks DISPATCH_ID"),
        ("wrong-authority", lambda r, n: _replace_relay_field(r, n, "AUTHORITY", "plan-only"), "selected origin DESIGN-planner-20260801-105000.md AUTHORITY is not design-only"),
        ("role-from-mismatch", lambda r, n: _replace_relay_field(r, n, "ROLE", "Pair Planner"), "selected origin DESIGN-planner-20260801-105000.md ROLE does not match FROM"),
    ]
    for suffix, mutation, detail in origin_matrix:
        add(
            f"X27-newer-malformed-origin-{suffix}",
            lambda repo, mutation=mutation: _latest_origin(repo, mutation),
            detail=detail,
        )

    def tier(repo: Path, origin_role: str, origin_from: str, review_role: str, review_from: str) -> None:
        _replace_relay_field(repo, ORIGIN_NAME, "ROLE", origin_role)
        _replace_relay_field(repo, ORIGIN_NAME, "FROM", origin_from)
        _replace_relay_field(repo, REVIEW_NAME, "ROLE", review_role)
        _replace_relay_field(repo, REVIEW_NAME, "FROM", review_from)

    tier_cases = [
        ("pair-domain", "Pair Planner", "m-1.pair-planner", "Domain Reviewer", "m-1.domain-reviewer"),
        ("domain-pair", "Domain Planner", "m-1.domain-planner", "Pair Implementer", "m-1.pair-implementer"),
        ("master-domain", "Master Planner", "m-1.master-planner", "Domain Reviewer", "m-1.domain-reviewer"),
    ]
    for suffix, origin_role, origin_from, review_role, review_from in tier_cases:
        reported_origin_role = {
            "pair-planner": "planner",
            "pair-implementer": "implementer",
        }.get(origin_from.split(".", 1)[1], origin_from.split(".", 1)[1])
        add(
            f"X28-wrong-tier-{suffix}",
            lambda repo, values=(origin_role, origin_from, review_role, review_from): tier(repo, *values),
            detail=f"selected review {REVIEW_NAME} is not a peer for selected origin role {reported_origin_role}",
        )

    add(
        "X29-origin-tie",
        lambda repo: _copy_relay(repo, ORIGIN_NAME, "ALT-DESIGN-planner-20260801-100000.md"),
        axis="ambiguity",
        detail="multiple latest DESIGN origins share timestamp 20260801-100000",
    )
    add(
        "X30-review-tie",
        lambda repo: _copy_relay(repo, REVIEW_NAME, "ALT-DESIGN-REVIEW-reviewer-20260801-110000.md"),
        axis="ambiguity",
        detail="multiple latest DESIGN-REVIEW carriers share timestamp 20260801-110000",
    )

    def rename(repo: Path, source: str, target: str) -> None:
        _relay(repo, source).rename(_relay(repo, target))

    add(
        "X31-unstamped-chain",
        lambda repo: rename(repo, REVIEW_NAME, "DESIGN-REVIEW-reviewer.md"),
        axis="ordering",
        detail="authority carrier DESIGN-REVIEW-reviewer.md lacks a filename timestamp",
    )
    add(
        "X32-review-before-design",
        lambda repo: rename(repo, REVIEW_NAME, "DESIGN-REVIEW-reviewer-20260801-090000.md"),
        axis="ordering",
        detail="selected review is not later than selected origin",
    )

    target_conflicts = [
        ("origin-phase", ORIGIN_NAME, "PHASE", "AUDIT"),
        ("origin-doc", ORIGIN_NAME, "DESIGN_DOC_ID", "d2"),
        ("review-phase", REVIEW_NAME, "PHASE", "AUDIT"),
        ("review-doc", REVIEW_NAME, "DESIGN_DOC_ID", "d2"),
        ("review-parent", REVIEW_NAME, "PARENT_DISPATCH_ID", "other-origin"),
    ]
    letter = ord("a")
    for label, carrier, key, conflicting in target_conflicts:
        for first in (False, True):
            suffix = chr(letter)
            letter += 1
            add(
                f"X33{suffix}-conflicting-{label}",
                lambda repo, carrier=carrier, key=key, conflicting=conflicting, first=first: _add_relay_field(repo, carrier, key, conflicting, first=first),
                detail=f"conflicting {key} occurrences in {carrier}",
            )

    cases.append(_build_case(
        dest,
        "X34-supersedes-inert",
        mutate_foreign=lambda repo: _add_relay_field(repo, REVIEW_NAME, "SUPERSEDES", ORIGIN_NAME, first=False),
        expected_errors=[],
    ))

    selected_conflicts = [
        ("origin-role", ORIGIN_NAME, "ROLE", "Pair Planner"),
        ("origin-from", ORIGIN_NAME, "FROM", "m-1.pair-planner"),
        ("origin-authority", ORIGIN_NAME, "AUTHORITY", "plan-only"),
        ("origin-dispatch", ORIGIN_NAME, "DISPATCH_ID", "other-origin"),
        ("origin-kind", ORIGIN_NAME, "DESIGN_RECORD_KIND", "audit-record"),
        ("origin-parent", ORIGIN_NAME, "PARENT_DISPATCH_ID", "unexpected-parent"),
        ("review-role", REVIEW_NAME, "ROLE", "Pair Implementer"),
        ("review-from", REVIEW_NAME, "FROM", "m-1.pair-implementer"),
        ("review-authority", REVIEW_NAME, "AUTHORITY", "plan-only"),
        ("review-dispatch", REVIEW_NAME, "DISPATCH_ID", "other-review"),
        ("review-kind", REVIEW_NAME, "DESIGN_RECORD_KIND", "audit-record"),
        ("review-verdict", REVIEW_NAME, "DESIGN_REVIEW_VERDICT", "must-revise"),
    ]
    def selected_conflict(
        repo: Path, carrier: str, key: str, conflicting: str, first: bool
    ) -> None:
        if carrier == ORIGIN_NAME and key == "PARENT_DISPATCH_ID":
            if first:
                _add_relay_field(repo, carrier, key, conflicting, first=True)
                _add_relay_field(repo, carrier, key, "expected-parent", first=False)
            else:
                _add_relay_field(repo, carrier, key, "expected-parent", first=True)
                _add_relay_field(repo, carrier, key, conflicting, first=False)
            return
        _add_relay_field(repo, carrier, key, conflicting, first=first)

    arm = 1
    for label, carrier, key, conflicting in selected_conflicts:
        for first in (False, True):
            add(
                f"X35-{arm:02d}-{label}-{'first' if first else 'last'}",
                lambda repo, carrier=carrier, key=key, conflicting=conflicting, first=first: selected_conflict(repo, carrier, key, conflicting, first),
                detail=f"conflicting {key} occurrences in selected carrier {carrier}",
            )
            arm += 1
    return cases


def _plan_source_prepare(mutation=None):
    def prepare(case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
        _unengaged_plan(fields)
        digest = hashlib.sha256(ARTIFACT.encode("utf-8")).hexdigest()
        fields.insert(-1, ("PLAN_LOCK_ID", f"designs/d1.md @ {digest}"))
        fields.insert(-1, ("PLAN_SOURCE_REPO", str(foreign)))
        fields.insert(-1, ("PLAN_SOURCE_COMMIT", str(_git(foreign, "rev-parse", "HEAD"))))
        if mutation is not None:
            mutation(case_root, foreign, fields)
    return prepare


def _plan_source_cases(dest: Path) -> list[Case]:
    cases: list[Case] = []

    def add(name: str, mutation=None, mutate_foreign=None, axis="declaration", detail="", errors=None):
        cases.append(_build_case(
            dest,
            name,
            mutate_foreign=mutate_foreign,
            prepare=_plan_source_prepare(mutation),
            local_relays=_local_chain(),
            expected_exit=1,
            expected_errors=errors if errors is not None else [_xerror(axis, detail)],
        ))

    cases.append(_build_case(
        dest,
        "XP1-valid",
        prepare=_plan_source_prepare(),
        local_relays=_local_chain(),
        expected_errors=[],
    ))
    add(
        "XP2-partial-repo-only",
        lambda _c, _r, fields: _remove(fields, "PLAN_SOURCE_COMMIT"),
        detail="missing PLAN_SOURCE_COMMIT",
    )
    add(
        "XP2b-partial-commit-only",
        lambda _c, _r, fields: _remove(fields, "PLAN_SOURCE_REPO"),
        detail="missing PLAN_SOURCE_REPO",
    )
    conflict_cases = [
        ("XP3a-conflicting-repo-last", "PLAN_SOURCE_REPO", "/other/repo", False),
        ("XP3b-conflicting-repo-first", "PLAN_SOURCE_REPO", "/other/repo", True),
        ("XP3c-conflicting-commit-last", "PLAN_SOURCE_COMMIT", "0" * 40, False),
        ("XP3d-conflicting-commit-first", "PLAN_SOURCE_COMMIT", "0" * 40, True),
    ]
    for name, key, value, first in conflict_cases:
        add(
            name,
            lambda _c, _r, fields, key=key, value=value, first=first: fields.insert(0 if first else len(fields) - 1, (key, value)),
            detail=f"conflicting {key} values",
        )
    add(
        "XP4-missing-blob",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "designs/absent.md"),
        axis="byte",
        detail="PLAN_LOCK_ID path does not exist at pinned commit",
    )

    def plan_tag(_case_root: Path, foreign: Path, fields: list[tuple[str, str]]) -> None:
        _git(foreign, "tag", "-a", "plan-tag", "-m", "fixture plan tag")
        _replace(fields, "PLAN_SOURCE_COMMIT", str(_git(foreign, "rev-parse", "plan-tag")))

    add(
        "XP5-tag-commit",
        plan_tag,
        axis="commit",
        detail="PLAN_SOURCE_COMMIT object type is tag, not commit",
    )

    def local_control(case_root: Path, _foreign: Path, fields: list[tuple[str, str]]) -> None:
        _unengaged_plan(fields)
        local = case_root / "build" / "local-plan.md"
        _write(local, "# Local plan fixture\n")
        fields.insert(-1, ("PLAN_LOCK_ID", str(local)))

    cases.append(_build_case(
        dest,
        "XP6-fields-absent-control",
        prepare=local_control,
        local_relays=_local_chain(),
        expected_errors=[],
    ))
    add(
        "XP7-empty-commit",
        lambda _c, _r, fields: _replace(fields, "PLAN_SOURCE_COMMIT", ""),
        detail="missing PLAN_SOURCE_COMMIT",
    )
    add(
        "XP7b-blank-repo",
        lambda _c, _r, fields: _replace(fields, "PLAN_SOURCE_REPO", ""),
        detail="missing PLAN_SOURCE_REPO",
    )
    add(
        "XP8-dotdot-traversal",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "../escape.md"),
        detail="PLAN_LOCK_ID path must be a literal relative path",
    )
    add(
        "XP9-pathspec-magic",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", ":(top)designs/d1.md"),
        detail="PLAN_LOCK_ID path must be a literal relative path",
    )
    add(
        "XP10-symlink-component",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "links/d1.md"),
        mutate_foreign=_symlink_component,
        detail="PLAN_LOCK_ID traverses symlink component links",
    )
    add(
        "XP11-non-blob",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "designs/nested"),
        mutate_foreign=lambda repo: _write(
            repo / "designs" / "nested" / "fixture.md", "# Nested fixture\n"
        ),
        axis="byte",
        detail="PLAN_LOCK_ID path resolves to tree, not blob",
    )
    add(
        "XP12-annotation-digest-mismatch",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", f"designs/d1.md @ {'0' * 64}"),
        axis="byte",
        detail="PLAN_LOCK_ID annotation digest does not match pinned blob bytes",
    )
    add(
        "XP13-absolute-path",
        lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "/etc/passwd"),
        detail="PLAN_LOCK_ID path must be a literal relative path",
    )
    def design_lock_path(
        _case_root: Path,
        _foreign: Path,
        fields: list[tuple[str, str]],
        value: str,
    ) -> None:
        _replace(fields, "DESIGN_LOCK_ID", value)
        _replace(fields, "PLAN_LOCK_ID", "opaque-plan-lock")
        _remove(fields, "DESIGN_RECORD_KIND")
        _remove(fields, "DESIGN_DOC_ID")

    cases.append(_build_case(
        dest,
        "XP14-design-lock-path-valid",
        prepare=_plan_source_prepare(
            lambda c, r, fields: design_lock_path(c, r, fields, "designs/d1.md")
        ),
        expected_errors=[],
    ))
    add(
        "XP15-design-lock-path-missing",
        lambda c, r, fields: design_lock_path(c, r, fields, "designs/absent.md"),
        axis="byte",
        detail="DESIGN_LOCK_ID path does not exist at pinned commit",
    )
    cases.append(_build_case(
        dest,
        "XP16-opaque-plan-lock",
        prepare=_plan_source_prepare(
            lambda _c, _r, fields: _replace(fields, "PLAN_LOCK_ID", "opaque-plan-lock")
        ),
        local_relays=_local_chain(),
        expected_errors=[],
    ))
    cases.append(_build_case(
        dest,
        "XP17-absent-plan-lock",
        prepare=_plan_source_prepare(
            lambda _c, _r, fields: _remove(fields, "PLAN_LOCK_ID")
        ),
        local_relays=_local_chain(),
        expected_errors=[],
    ))
    return cases


def materialize(dest: Path) -> list[Case]:
    """Create all Git-backed cases under dest and return their expectations."""
    dest.mkdir(parents=True, exist_ok=True)
    pairing = "DESIGN_SHA256 declared with no DESIGN_ARTIFACT to locate the artifact"
    cases = [
        _build_case(dest, "XV-valid-edge", expected_errors=[]),
        _build_case(dest, "X0a-fields-absent-control", mutate_plan=_unengaged_plan, local_relays=_local_chain(), expected_errors=[]),
        _build_case(dest, "X0b-no-git-on-path-control", mutate_plan=_unengaged_plan, local_relays=_local_chain(), expected_errors=[]),
        _build_case(
            dest,
            "X1-incomplete-declaration",
            mutate_plan=lambda fields: (
                [_remove(fields, key) for key in (
                    "DESIGN_LOCK_ID", "DESIGN_RECORD_KIND", "DESIGN_DOC_ID", "DESIGN_OWNER",
                    "DESIGN_SOURCE_COMMIT", "DESIGN_SOURCE_ROOT", "DESIGN_SOURCE_PATH", "DESIGN_SHA256",
                )]
            ),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "missing DESIGN_SOURCE_COMMIT, DESIGN_SOURCE_ROOT, DESIGN_SOURCE_PATH, DESIGN_OWNER, DESIGN_DOC_ID, DESIGN_SHA256, DESIGN_LOCK_ID")],
        ),
        _build_case(
            dest,
            "X2-missing-lock",
            mutate_plan=lambda fields: _remove(fields, "DESIGN_LOCK_ID"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "missing DESIGN_LOCK_ID")],
        ),
        _build_case(
            dest,
            "X3-wrong-kind",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_RECORD_KIND", "audit-record"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "DESIGN_RECORD_KIND must be design-doc under a declared edge")],
        ),
        _build_case(
            dest,
            "X4-conflicting-owner",
            mutate_plan=lambda fields: fields.insert(-1, ("DESIGN_OWNER", "m-2")),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "conflicting DESIGN_OWNER values")],
        ),
        _build_case(
            dest,
            "X5-local-foreign-ambiguity",
            local_relays={"DESIGN-pair-planner-20260817-100000.md": _local_chain()["DESIGN-pair-planner-20260817-100000.md"].replace("s4.", "m-1.")},
            expected_exit=1,
            expected_errors=[_xerror("ambiguity", "same-owner local DESIGN carries DESIGN_DOC_ID d1")],
        ),
        _build_case(
            dest,
            "X5b-local-nonmatching-control",
            local_relays={"DESIGN-pair-planner-20260817-100000.md": _local_chain()["DESIGN-pair-planner-20260817-100000.md"]},
            expected_errors=[],
        ),
        _build_case(
            dest,
            "X6-absent-repo",
            prepare=_absent_repo,
            expected_exit=1,
            expected_errors=[_xerror("repository", "DESIGN_SOURCE_REPO does not name an existing directory")],
        ),
        _build_case(
            dest,
            "X6b-relative-repo-positive",
            prepare=_relative_repo,
            expected_errors=[],
        ),
        _build_case(
            dest,
            "X7-not-a-repo",
            prepare=_not_repo,
            expected_exit=1,
            expected_errors=[_xerror("repository", "DESIGN_SOURCE_REPO is not a Git repository top level")],
        ),
        _build_case(
            dest,
            "X7b-repo-subdirectory",
            prepare=_repo_subdirectory,
            expected_exit=1,
            expected_errors=[_xerror("repository", "DESIGN_SOURCE_REPO resolves inside a Git repository but not to its top level")],
        ),
        _build_case(
            dest,
            "X8-short-commit",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_COMMIT", "0123456789ab"),
            expected_exit=1,
            expected_errors=[_xerror("commit", "DESIGN_SOURCE_COMMIT must be a full lowercase 40-hex object id")],
        ),
        _build_case(
            dest,
            "X9-unknown-commit",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_COMMIT", "0" * 40),
            expected_exit=1,
            expected_errors=[_xerror("commit", "DESIGN_SOURCE_COMMIT object is unavailable")],
        ),
        _build_case(
            dest,
            "X10-tag-object",
            prepare=_tag_commit,
            expected_exit=1,
            expected_errors=[_xerror("commit", "DESIGN_SOURCE_COMMIT object type is tag, not commit")],
        ),
        _build_case(
            dest,
            "X11-absolute-root",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_ROOT", "/etc"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "DESIGN_SOURCE_ROOT must be a literal relative path")],
        ),
        _build_case(
            dest,
            "X12-dotdot-path",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", "../escape.md"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "DESIGN_SOURCE_PATH must be a literal relative path")],
        ),
        _build_case(
            dest,
            "X13-magic-path",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", ":(top)designs/d1.md"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "DESIGN_SOURCE_PATH must be a literal relative path")],
        ),
        _build_case(
            dest,
            "X14-missing-root",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_ROOT", "absent-relays"),
            expected_exit=1,
            expected_errors=[_xerror("authority-population", "DESIGN_SOURCE_ROOT does not exist at pinned commit")],
        ),
        _build_case(
            dest,
            "X15-root-not-tree",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_ROOT", "designs/d1.md"),
            expected_exit=1,
            expected_errors=[_xerror("authority-population", "DESIGN_SOURCE_ROOT resolves to blob, not tree")],
        ),
        _build_case(
            dest,
            "X16-missing-blob",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", "designs/absent.md"),
            expected_exit=1,
            expected_errors=[_xerror("byte", "DESIGN_SOURCE_PATH does not exist at pinned commit")],
        ),
        _build_case(
            dest,
            "X17-path-is-tree",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", "designs"),
            expected_exit=1,
            expected_errors=[_xerror("byte", "DESIGN_SOURCE_PATH resolves to tree, not blob")],
        ),
        _build_case(
            dest,
            "X18-symlink-component",
            mutate_foreign=_symlink_component,
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", "links/d1.md"),
            expected_exit=1,
            expected_errors=[_xerror("declaration", "DESIGN_SOURCE_PATH traverses symlink component links")],
        ),
        _build_case(
            dest,
            "X18b-bytes-preserving-name",
            mutate_foreign=_quoted_utf8_component,
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SOURCE_PATH", 'desi"gn-é/d1.md'),
            expected_errors=[],
        ),
        _build_case(
            dest,
            "X19-digest-mismatch",
            prepare=_second_commit_digest,
            expected_exit=1,
            expected_errors=[_xerror("byte", "DESIGN_SHA256 does not match pinned blob bytes")],
        ),
        _build_case(
            dest,
            "X20-malformed-digest",
            mutate_plan=lambda fields: _replace(fields, "DESIGN_SHA256", "0" * 63),
            expected_exit=1,
            expected_errors=[
                f"{PLAN_NAME}: DESIGN_SHA256 value {'0' * 63!r} is not a 64-hex lowercase sha256 digest",
                _xerror("byte", "DESIGN_SHA256 must be a full lowercase 64-hex digest"),
            ],
        ),
        _build_case(
            dest,
            "X36-digest-pairing-control",
            mutate_plan=_digest_control,
            expected_exit=1,
            expected_errors=[f"{PLAN_NAME}: {pairing}"],
        ),
        _build_case(
            dest,
            "X37-boundary-control",
            mutate_plan=_wrong_phase_boundary,
            expected_exit=1,
            expected_errors=[f"{PLAN_NAME}: {pairing}"],
        ),
        _build_case(
            dest,
            "X38-noncanonical-role-engages",
            mutate_plan=lambda fields: _replace(fields, "ROLE", "Domain Planner"),
            expected_exit=1,
            expected_errors=[
                f"{PLAN_NAME}: ROLE/FROM mismatch: ROLE='Domain Planner' but FROM='s4.pair-planner'; do not proxy-author another seat's relay",
                _xerror("declaration", "declared edge carrier ROLE must be pair-Planner class"),
            ],
        ),
        _build_case(
            dest,
            "X39-two-from-engages",
            mutate_plan=lambda fields: fields.insert(-1, ("FROM", "s5.pair-planner")),
            expected_exit=1,
            expected_errors=[
                f"{PLAN_NAME}: DESIGN_SHA256 declared with no DESIGN_ARTIFACT to locate the artifact",
                f"{PLAN_NAME}: FROM carries 2 distinct values across 2 occurrences; an authority-critical field is fail-closed unless exactly one distinct value is present (DD-v29-master-authority-20260809 rule 5)",
                _xerror("declaration", "declared edge carrier FROM must be exactly one pair-Planner-class address"),
            ],
        ),
        _build_case(
            dest,
            "X40-non-pair-owner-engages",
            mutate_plan=lambda fields: (
                _replace(fields, "ROLE", "Orchestrator Planner"),
                _replace(fields, "FROM", "v291.orchestrator-planner"),
            ),
            expected_exit=1,
            expected_errors=[
                f"{PLAN_NAME}: DESIGN_SHA256 declared with no DESIGN_ARTIFACT to locate the artifact",
                _xerror("declaration", "declared edge carrier ROLE must be pair-Planner class"),
            ],
        ),
        _build_case(
            dest,
            "X41-index-excluded",
            mutate_foreign=lambda repo: _write(
                repo / "relays" / "INDEX.md",
                "PHASE: DESIGN\nDESIGN_DOC_ID: d1\n",
            ),
            expected_errors=[],
        ),
        _build_case(
            dest,
            "X42-legacy-alias-chain",
            mutate_foreign=lambda repo: (
                _replace_relay_field(repo, ORIGIN_NAME, "ROLE", "Planner"),
                _replace_relay_field(repo, ORIGIN_NAME, "FROM", "m-1.pair-planner"),
                _replace_relay_field(repo, REVIEW_NAME, "ROLE", "Implementer"),
                _replace_relay_field(repo, REVIEW_NAME, "FROM", "m-1.pair-implementer"),
            ),
            expected_errors=[],
        ),
        _build_case(
            dest,
            "X43-fifo-object-timeout",
            prepare=_fifo_object,
            expected_exit=1,
            expected_errors=[_xerror("commit", "Git object query timed out")],
        ),
        _build_case(
            dest,
            "X44-non-ascii-tree-mode",
            prepare=_non_ascii_tree_mode,
            expected_exit=1,
            expected_errors=[_xerror("repository", "pinned tree object has malformed entry bytes")],
        ),
        _build_case(
            dest,
            "X45-deep-tree",
            prepare=_deep_tree,
            expected_exit=1,
            expected_errors=[
                _xerror("repository", "DESIGN_SOURCE_ROOT exceeds maximum tree depth 256")
            ],
        ),
    ]
    cases.extend(_authority_cases(dest))
    cases.extend(_plan_source_cases(dest))
    return cases


def _commit_ids(cases: list[Case]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for label, relay_root, _expected_exit, _expected_errors in cases:
        foreign = relay_root.parents[2] / "pdc"
        ids[label] = str(_git(foreign, "rev-parse", "HEAD"))
    return ids


def self_check() -> int:
    with tempfile.TemporaryDirectory(prefix="xrootfixgen-a.") as first_raw:
        first = materialize(Path(first_raw))
        first_ids = _commit_ids(first)
    with tempfile.TemporaryDirectory(prefix="xrootfixgen-b.") as second_raw:
        second = materialize(Path(second_raw))
        second_ids = _commit_ids(second)
    if first_ids != second_ids:
        raise SystemExit("xrootfixgen: nondeterministic commit ids")
    print(f"xrootfixgen-ok cases={len(first_ids)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if not args.self_check:
        parser.error("--self-check is required")
    return self_check()


if __name__ == "__main__":
    raise SystemExit(main())
