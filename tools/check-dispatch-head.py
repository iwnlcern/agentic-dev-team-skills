#!/usr/bin/env python3
"""Resolve one dispatch relay's uniquely declared accepted Git head."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import re
from pathlib import Path


VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$")


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
    print(f"dispatch-head: {message}", file=__import__("sys").stderr)
    return 1


def run_gate(cycle_dir: Path, dispatch_id: str) -> int:
    try:
        linter = load_linter()
        matches: list[tuple[Path, str]] = []
        for path in sorted(cycle_dir.rglob("*.md")):
            if not path.is_file():
                continue
            text = linter.read(path)
            ids = occurrences(linter, text, "DISPATCH_ID")
            if len(ids) == 1 and ids[0] == dispatch_id:
                matches.append((path, text))
        if len(matches) != 1:
            return refuse(
                f"{len(matches)} relays parse to dispatch id {dispatch_id!r}; required exactly 1"
            )
        heads = occurrences(linter, matches[0][1], "ACCEPTED_HEAD")
        if len(heads) != 1:
            return refuse(
                "dispatch relay: ACCEPTED_HEAD occurs "
                f"{len(heads)} times in operational text; required exactly 1"
            )
        if re.fullmatch(r"[0-9a-f]{40}", heads[0]) is None:
            return refuse("ACCEPTED_HEAD is not a 40-hex lowercase commit sha")
        print(heads[0])
        return 0
    except (OSError, RuntimeError) as exc:
        return refuse(str(exc))


def selftest(root: Path) -> int:
    fixtures = sorted(root.glob("H[0-5]-*"))
    for fixture in fixtures:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = run_gate(fixture, "fx-impl-2")
        expected_stderr = (fixture / "expected-reason.txt").read_text(
            encoding="utf-8"
        )
        if fixture.name == "H0-valid":
            good = (
                status == 0
                and stdout.getvalue() == VALID_SHA + "\n"
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
    print(f"dispatch-head selftest: {len(fixtures)}/{len(fixtures)} passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cycle_dir", nargs="?", type=Path)
    parser.add_argument("dispatch_id", nargs="?")
    parser.add_argument("--selftest", type=Path)
    args = parser.parse_args()
    if args.selftest:
        return selftest(args.selftest)
    if args.cycle_dir is None or args.dispatch_id is None:
        parser.error("gate mode requires CYCLE_DIR DISPATCH_ID")
    return run_gate(args.cycle_dir, args.dispatch_id)


if __name__ == "__main__":
    raise SystemExit(main())
