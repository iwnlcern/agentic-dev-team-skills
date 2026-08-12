#!/usr/bin/env python3
"""Produce a compact record for one suite invocation."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_SUITE = (sys.executable, "tools/check-relay-lint-fixtures.py")
RESULT_LINE = re.compile(r"\b(PASS|FAIL)\s*$")


def parse_results(output: str) -> tuple[int, int]:
    """Count fixture result lines emitted by a suite."""
    outcomes = [RESULT_LINE.search(line) for line in output.splitlines()]
    return (
        sum(match is not None and match.group(1) == "PASS" for match in outcomes),
        sum(match is not None and match.group(1) == "FAIL" for match in outcomes),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="suite command after --; defaults to the base fixture suite",
    )
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.spec.is_file():
        raise SystemExit(f"spec does not exist: {args.spec}")
    command = tuple(args.command) or DEFAULT_SUITE
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    passed, failed = parse_results(completed.stdout + completed.stderr)
    print(f"PASS={passed} FAIL={failed} exit={completed.returncode}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
