#!/usr/bin/env python3
"""Authenticate Task 11 fold closure, relay lineage, pins, and artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import re
import shutil
import tempfile
from pathlib import Path


LIVE_PARENT_ID = "v29-detection-impl-2"
FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$")
LOCK_RE = re.compile(r"^(.+?) @ sha256 ([0-9a-f]{64})$")


def load_linter():
    path = Path(__file__).with_name("relay-lint.py")
    spec = importlib.util.spec_from_file_location("task11_relay_lint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load relay-lint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def occurrences(linter, text: str, key: str) -> list[str]:
    values: list[str] = []
    for line in linter.operational_token_text(text).splitlines():
        match = FIELD_RE.match(line.rstrip())
        if match and match.group(1) == key:
            values.append(match.group(2).strip())
    return values


def refuse(message: str) -> int:
    print(f"t11-closure: {message}", file=__import__("sys").stderr)
    return 1


def one_value(linter, text: str, label: str, key: str) -> tuple[str | None, int | None]:
    values = occurrences(linter, text, key)
    if len(values) != 1:
        return None, refuse(
            f"{label}: {key} occurs {len(values)} times in operational text; required exactly 1"
        )
    if values[0] == "":
        return None, refuse(f"{label}: {key} value is empty")
    return values[0], None


def require_value(label: str, key: str, got: str, want: str) -> int | None:
    if got != want:
        return refuse(f"{label}: {key} is {got!r}; required {want!r}")
    return None


def exact_lint_ok(linter, path: Path) -> bool:
    return linter.lint_file(path, freshness=False).ok


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_by_id(linter, cycle_dir: Path, dispatch_id: str) -> list[tuple[Path, str]]:
    matches: list[tuple[Path, str]] = []
    for path in sorted(cycle_dir.rglob("*.md")):
        if not path.is_file():
            continue
        text = linter.read(path)
        if linter.header_fields(text).get("DISPATCH_ID") == dispatch_id:
            matches.append((path, text))
    return matches


def run_gate(
    closure: Path,
    closing: Path,
    results_root: Path,
    design_id: str,
    design_path: Path,
    design_sha: str,
    plan_root: Path,
    review_parent: str,
    *,
    expected_parent_id: str = LIVE_PARENT_ID,
) -> int:
    try:
        linter = load_linter()
        closure_lines = closure.read_text(encoding="utf-8").splitlines()
        if len(closure_lines) != 2:
            return refuse("closure artifact is not exactly two lines")
        expected_relay = f"relay: v29-detection/{closing.name}"
        if closure_lines != [expected_relay, "verdict: fold-complete"]:
            return refuse("closure artifact does not name the closing relay and fold-complete verdict")

        closing_text = linter.read(closing)
        closing_values: dict[str, str] = {}
        for key in ("PHASE", "FROM", "DISPATCH_ID", "PARENT_DISPATCH_ID", "STATUS"):
            value, error = one_value(linter, closing_text, "closing relay", key)
            if error is not None:
                return error
            closing_values[key] = value or ""
        for key, want in (
            ("PHASE", "REVIEW-FOLD"),
            ("FROM", "v29-faults.implementer"),
            ("DISPATCH_ID", "v29-detection-t11-fold"),
            ("PARENT_DISPATCH_ID", expected_parent_id),
            ("STATUS", "fold-complete"),
        ):
            error = require_value("closing relay", key, closing_values[key], want)
            if error is not None:
                return error
        if not exact_lint_ok(linter, closing):
            return refuse("closing relay fails exact-file lint")

        parents = resolve_by_id(linter, closing.parent, expected_parent_id)
        if len(parents) != 1:
            return refuse(
                f"{len(parents)} relays parse to parent dispatch id {expected_parent_id!r}; required exactly 1"
            )
        parent_path, parent_text = parents[0]
        parent_values: dict[str, str] = {}
        for key in (
            "DISPATCH_ID",
            "PARENT_DISPATCH_ID",
            "PHASE",
            "FROM",
            "TO",
            "PLAN_LOCK_ID",
            "DESIGN_LOCK_ID",
        ):
            value, error = one_value(linter, parent_text, "parent relay", key)
            if error is not None:
                return error
            parent_values[key] = value or ""
        for key, want in (
            ("DISPATCH_ID", expected_parent_id),
            ("PHASE", "IMPL"),
            ("FROM", "v29-faults.planner"),
            ("TO", "v29-faults.implementer"),
        ):
            error = require_value("parent relay", key, parent_values[key], want)
            if error is not None:
                return error
        if not linter.own_line_dispatch_present(parent_text):
            return refuse("parent relay: no bare own-line dispatch token found")
        if not exact_lint_ok(linter, parent_path):
            return refuse("parent relay fails exact-file lint")

        review_id = parent_values["PARENT_DISPATCH_ID"]
        reviews = resolve_by_id(linter, closing.parent, review_id)
        if len(reviews) != 1:
            return refuse(
                f"{len(reviews)} relays parse to approving review id {review_id!r}; required exactly 1"
            )
        review_path, review_text = reviews[0]
        review_values: dict[str, str] = {}
        for key in (
            "DISPATCH_ID",
            "PHASE",
            "FROM",
            "TO",
            "PLAN_REVIEW_VERDICT",
            "PARENT_DISPATCH_ID",
            "PLAN_LOCK_ID",
        ):
            value, error = one_value(linter, review_text, "review relay", key)
            if error is not None:
                return error
            review_values[key] = value or ""
        for key, want in (
            ("DISPATCH_ID", review_id),
            ("PHASE", "PLAN-REVIEW"),
            ("FROM", "v29-faults.implementer"),
            ("TO", "v29-faults.planner"),
            ("PLAN_REVIEW_VERDICT", "approve"),
            ("PARENT_DISPATCH_ID", review_parent),
        ):
            error = require_value("review relay", key, review_values[key], want)
            if error is not None:
                return error
        if not exact_lint_ok(linter, review_path):
            return refuse("review relay fails exact-file lint")

        if parent_values["PLAN_LOCK_ID"] != review_values["PLAN_LOCK_ID"]:
            return refuse("parent PLAN_LOCK_ID does not match the approving review's PLAN_LOCK_ID")
        lock_match = LOCK_RE.fullmatch(review_values["PLAN_LOCK_ID"])
        if lock_match is None:
            return refuse("approving review PLAN_LOCK_ID has invalid path-plus-sha256 syntax")
        lock_path = Path(lock_match.group(1))
        declared_plan_sha = lock_match.group(2)
        if lock_path.is_absolute():
            return refuse("plan lock path is absolute")
        if ".." in lock_path.parts:
            return refuse("plan lock path escapes the plan root")
        resolved_root = plan_root.resolve()
        resolved_plan = (resolved_root / lock_path).resolve()
        if resolved_plan != resolved_root and resolved_root not in resolved_plan.parents:
            return refuse("plan lock path escapes the plan root")
        if sha256(resolved_plan) != declared_plan_sha:
            return refuse("plan file digest mismatch: recomputed sha256 does not equal the declared lock")

        if parent_values["DESIGN_LOCK_ID"] != design_id:
            return refuse(
                "parent DESIGN_LOCK_ID is "
                f"{parent_values['DESIGN_LOCK_ID']!r}; required {design_id!r}"
            )
        if sha256(design_path) != design_sha:
            return refuse("design file digest mismatch: recomputed sha256 does not equal the design pin")

        identity = results_root / "v29-detection-corpus-identity-final.txt"
        if identity.read_bytes() != b"identity-check-exit: 0\n":
            return refuse("snapshot identity artifact is not byte-exact success")
        snapshot_lines = (
            results_root / "v29-detection-snapshot-path.txt"
        ).read_text(encoding="utf-8").splitlines()
        if (
            len(snapshot_lines) != 2
            or not Path(snapshot_lines[0]).is_absolute()
            or re.fullmatch(r"\d+ \d+", snapshot_lines[1]) is None
        ):
            return refuse("snapshot path record does not match the required two-line schema")

        print("t11-closure: PASS")
        return 0
    except (OSError, RuntimeError) as exc:
        return refuse(str(exc))


def read_pins(path: Path) -> tuple[str, str, str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    design_path, design_sha = lines[0].removeprefix("design: ").rsplit(" ", 1)
    design_id = lines[1].removeprefix("design-id: ")
    review_parent = lines[2].removeprefix("review-parent: ")
    return design_path, design_sha, design_id, review_parent


def selftest(root: Path) -> int:
    fixtures = [root / "G1-valid", *sorted(root.glob("R[0-9][0-9]-*"))]
    for fixture in fixtures:
        with tempfile.TemporaryDirectory(prefix="t11closure.") as tmp:
            copied = Path(tmp) / fixture.name
            shutil.copytree(fixture, copied)
            if fixture.name.startswith("R26-"):
                replacement = str(copied.resolve())
                for name in ("parent.md", "review.md"):
                    path = copied / name
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            "__ABS_FIXTURE_ROOT__", replacement
                        ),
                        encoding="utf-8",
                    )
            (copied / "v29-detection-corpus-identity-final.txt").write_text(
                "identity-check-exit: 0\n", encoding="utf-8"
            )
            (copied / "v29-detection-snapshot-path.txt").write_text(
                f"{copied.resolve()}\n1 2\n", encoding="utf-8"
            )
            design_rel, design_sha, design_id, review_parent = read_pins(
                copied / "pins.txt"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = run_gate(
                    copied / "closure.txt",
                    copied / "closing.md",
                    copied,
                    design_id,
                    copied / design_rel,
                    design_sha,
                    copied,
                    review_parent,
                    expected_parent_id="fx-impl-2",
                )
            expected_stderr = (copied / "expected-reason.txt").read_text(
                encoding="utf-8"
            )
            if fixture.name == "G1-valid":
                good = (
                    status == 0
                    and stdout.getvalue() == "t11-closure: PASS\n"
                    and stderr.getvalue() == ""
                )
            else:
                good = (
                    status != 0
                    and stdout.getvalue() == ""
                    and stderr.getvalue() == expected_stderr
                )
            if not good:
                print(
                    f"selftest: {fixture.name}: status={status} "
                    f"stdout={stdout.getvalue()!r} stderr={stderr.getvalue()!r}",
                    file=__import__("sys").stderr,
                )
                return 1
    print(f"t11-closure selftest: {len(fixtures)}/{len(fixtures)} passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("closure", nargs="?", type=Path)
    parser.add_argument("closing", nargs="?", type=Path)
    parser.add_argument("results_root", nargs="?", type=Path)
    parser.add_argument("--design-id")
    parser.add_argument("--design-pin", nargs=2, metavar=("PATH", "SHA256"))
    parser.add_argument("--plan-root", type=Path)
    parser.add_argument("--review-parent")
    parser.add_argument("--selftest", type=Path)
    args = parser.parse_args()
    if args.selftest:
        return selftest(args.selftest)
    if not all(
        (
            args.closure,
            args.closing,
            args.results_root,
            args.design_id,
            args.design_pin,
            args.plan_root,
            args.review_parent,
        )
    ):
        parser.error("gate mode requires closure, closing, results root, and all pins")
    return run_gate(
        args.closure,
        args.closing,
        args.results_root,
        args.design_id,
        Path(args.design_pin[0]),
        args.design_pin[1],
        args.plan_root,
        args.review_parent,
    )


if __name__ == "__main__":
    raise SystemExit(main())
