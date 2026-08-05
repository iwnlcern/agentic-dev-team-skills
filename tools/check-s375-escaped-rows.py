#!/usr/bin/env python3
"""Triangulate Leg-D raw oracle, candidate parser, and candidate CLI output."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path


def fail(message: str) -> int:
    print(f"s375-escaped-rows: {message}", file=sys.stderr)
    return 1


def load_candidate():
    path = Path(__file__).with_name("relay-lint.py")
    spec = importlib.util.spec_from_file_location("task11_candidate_lint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load relay-lint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def escaped_pipe(raw: str) -> bool:
    slash_run = 0
    for byte in raw:
        if byte == "\\":
            slash_run += 1
        else:
            if byte == "|" and slash_run % 2:
                return True
            slash_run = 0
    return False


def load_oracle(path: Path) -> dict[str, object]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("oracle capture contains no JSON payload")


def cli_arity_lines(path: Path) -> Counter[int]:
    found: Counter[int] = Counter()
    pattern = re.compile(
        r"^ERROR .*/?INDEX\.md: line (\d+): row has \d+ cells, header declares \d+; "
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            found[int(match.group(1))] += 1
    return found


def compare(index: Path, oracle_capture: Path, cli_capture: Path) -> int:
    candidate = load_candidate()
    oracle = load_oracle(oracle_capture)
    header_line = int(oracle["header_line"])
    header_arity = int(oracle["header_arity"])
    oracle_rows = {int(row["line"]): row for row in oracle["rows"]}

    lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    candidate_rows: dict[int, dict[str, object]] = {}
    for lineno, line in enumerate(lines, 1):
        if lineno <= header_line or not line.lstrip().startswith("|"):
            continue
        cells = candidate.split_index_cells(line.strip())
        if cells and set(cells[0]) <= set("-: "):
            continue
        if len(cells) != header_arity:
            kind = "arity-error"
        elif escaped_pipe(line):
            kind = "escaped-valid"
        else:
            kind = "ordinary-valid"
        candidate_rows[lineno] = {
            "line": lineno,
            "class": kind,
            "cells": cells,
            "raw": line,
        }

    if set(candidate_rows) != set(oracle_rows):
        return fail("candidate and oracle raw-row populations differ")
    for lineno in sorted(oracle_rows):
        expected = oracle_rows[lineno]
        actual = candidate_rows[lineno]
        if actual["class"] != expected["class"]:
            return fail(
                f"line {lineno} class mismatch: candidate={actual['class']} oracle={expected['class']}"
            )
        if expected["class"] == "escaped-valid" and actual["cells"] != expected["cells"]:
            return fail(f"line {lineno} restored escaped-pipe content differs")

    parser_arity = Counter(
        lineno
        for lineno, row in candidate_rows.items()
        if row["class"] == "arity-error"
    )
    cli_arity = cli_arity_lines(cli_capture)
    if parser_arity != cli_arity:
        return fail("candidate parser arity rows differ from candidate CLI capture")
    counts = Counter(str(row["class"]) for row in candidate_rows.values())
    print(
        "LEG D PASS: "
        f"population={len(candidate_rows)} arity-error={counts['arity-error']} "
        f"escaped-valid={counts['escaped-valid']} ordinary-valid={counts['ordinary-valid']}"
    )
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: check-s375-escaped-rows.py INDEX ORACLE_CAPTURE CLI_CAPTURE",
            file=sys.stderr,
        )
        return 2
    try:
        return compare(*(Path(value) for value in sys.argv[1:]))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
