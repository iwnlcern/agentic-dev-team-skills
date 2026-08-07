#!/usr/bin/env python3
"""Independent raw-byte index oracle and Task 11 capture comparators.

The oracle deliberately imports no candidate linter code. ``--emit`` writes one
section per explicit MODE/PATH pair and always exits zero when the oracle itself
ran successfully. ``--compare`` checks each captured candidate section against
its mode-matched oracle section, including the candidate exit status.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


TIME_FORMATS = (
    "%Y%m%d-%H%M%S",
    "%Y%m%d-%H%M%SZ",
    "%Y%m%dT%H%M%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
)
MARKER_RE = re.compile(
    r"<!--\s*relay-lint:\s*monotonic-from\s+(\S[^>]*?)\s*-->", re.I
)
FILENAME_RE = re.compile(r"(\d{8})[-T]?(\d{6})(Z?)")
FINDING_RE = re.compile(r"^(WARN|ERROR) ([^:]+): (.*)$")


@dataclass
class OracleResult:
    warnings: list[str]
    errors: list[str]


def fail(message: str) -> int:
    print(f"index-oracle: {message}", file=sys.stderr)
    return 1


def split_cells(raw: str) -> list[str]:
    """Independent odd/even-backslash Markdown table splitter."""
    cells: list[str] = []
    current: list[str] = []
    slash_run = 0
    for byte in raw:
        if byte == "\\":
            slash_run += 1
            current.append(byte)
            continue
        if byte == "|":
            if slash_run % 2:
                current.pop()
                current.append("|")
            else:
                cells.append("".join(current).strip())
                current = []
            slash_run = 0
            continue
        slash_run = 0
        current.append(byte)
    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def has_escaped_pipe(raw: str) -> bool:
    slash_run = 0
    for byte in raw:
        if byte == "\\":
            slash_run += 1
        else:
            if byte == "|" and slash_run % 2:
                return True
            slash_run = 0
    return False


def parse_time(raw: str) -> dt.datetime | None:
    for fmt in TIME_FORMATS:
        try:
            return dt.datetime.strptime(raw.strip(), fmt)
        except ValueError:
            pass
    return None


def filename_time(name: str) -> tuple[str, dt.datetime | None, str, bool]:
    match = FILENAME_RE.search(Path(name).stem)
    if match is None:
        return "absent", None, "", False
    raw = f"{match.group(1)}-{match.group(2)}"
    try:
        parsed = dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return "invalid", None, raw, bool(match.group(3))
    return "ok", parsed, raw, bool(match.group(3))


def oracle(path: Path, audit: bool) -> OracleResult:
    text = path.read_bytes().decode("utf-8", errors="replace")
    lines = text.splitlines()
    warnings: list[str] = []
    errors: list[str] = []

    marker_line = 0
    marker_time: dt.datetime | None = None
    _markers = list(MARKER_RE.finditer(text))
    first_marker = _markers[-1] if _markers else None
    if first_marker:
        marker_time = parse_time(first_marker.group(1))
        for lineno, line in enumerate(lines, 1):
            if MARKER_RE.search(line):
                marker_line = lineno
        if marker_time is None:
            errors.append(
                f"monotonic-from marker value {first_marker.group(1)!r} is not a valid timestamp"
            )

    rows: list[tuple[int, str, dt.datetime | None, str]] = []
    header_arity: int | None = None
    grandfathered_arity: list[tuple[int, int]] = []
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_cells(stripped)
        if len(cells) < 2:
            continue
        if cells[0].lower() == "time":
            if header_arity is None:
                header_arity = len(cells)
            continue
        if set(cells[0]) <= set("-: "):
            continue
        if header_arity is not None and len(cells) != header_arity:
            message = (
                f"line {lineno}: row has {len(cells)} cells, header declares {header_arity}; "
                "a literal | in cell prose must be escaped as \\|"
            )
            if lineno > marker_line:
                errors.append(message)
            else:
                grandfathered_arity.append((lineno, len(cells)))
        rows.append((lineno, cells[0], parse_time(cells[0]), cells[-1]))

    if header_arity is None:
        warnings.append("no header row; arity not checked")
    if not rows:
        errors.append(f"no index rows found in {path}")
        return OracleResult(warnings, errors)

    scoped = [row for row in rows if row[0] > marker_line]
    grandfathered = [row for row in rows if row[0] <= marker_line]
    for lineno, raw, parsed, _ in scoped:
        if parsed is None:
            errors.append(f"line {lineno}: index time {raw!r} is not a valid timestamp")
    parsed_rows = [row for row in scoped if row[2] is not None]
    for current, previous in zip(parsed_rows[1:], parsed_rows[:-1]):
        lineno, raw, parsed, _ = current
        _, previous_raw, previous_time, _ = previous
        if parsed < previous_time:
            errors.append(
                f"line {lineno}: index time {raw} precedes the previous row {previous_raw}; "
                "an append-only index must be non-decreasing"
            )
    if marker_time is not None:
        for lineno, raw, parsed, _ in parsed_rows:
            if parsed < marker_time:
                errors.append(
                    f"line {lineno}: index time {raw} predates the monotonic-from boundary "
                    f"{marker_time.strftime('%Y%m%d-%H%M%S')}"
                )
    for lineno, raw, parsed, file_cell in parsed_rows:
        status, file_time, file_raw, _ = filename_time(file_cell)
        if status == "invalid":
            errors.append(
                f"line {lineno}: referenced file timestamp {file_raw} is not a real date/time"
            )
        elif status == "ok" and file_time != parsed:
            errors.append(
                f"line {lineno}: index time {raw} disagrees with its filename timestamp {file_raw}"
            )
    if parsed_rows:
        lineno, raw, parsed, file_cell = parsed_rows[-1]
        _, _, _, is_utc = filename_time(file_cell)
        now = (
            dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            if is_utc
            else dt.datetime.now()
        )
        if parsed > now:
            ahead = (parsed - now).total_seconds() / 60.0
            magnitude = f"{ahead / 1440.0:.1f} days" if ahead >= 1440 else f"{ahead:.0f} min"
            zone = "UTC" if is_utc else "local"
            errors.append(
                f"line {lineno}: newest index time {raw} is {magnitude} ahead of the {zone} clock "
                f"(now {now.strftime('%Y%m%d-%H%M%S')}); an append-only index cannot carry a row "
                "stamped later than the append that wrote it"
            )

    if audit:
        historical = [row for row in grandfathered if row[2] is not None]
        unparsed = [(line, raw) for line, raw, parsed, _ in grandfathered if parsed is None]
        decreases = [
            (current[0], previous[1], current[1])
            for current, previous in zip(historical[1:], historical[:-1])
            if current[2] < previous[2]
        ]
        warnings.append(
            f"grandfathered history: {len(grandfathered)} rows before the marker, "
            f"{len(decreases)} non-monotonic, {len(unparsed)} unparseable"
        )
        for lineno, previous_raw, raw in decreases:
            warnings.append(
                f"line {lineno}: (grandfathered) {previous_raw} -> {raw} decreases"
            )
        for lineno, raw in unparsed:
            warnings.append(f"line {lineno}: (grandfathered) unparseable time {raw!r}")
        for lineno, arity in grandfathered_arity:
            warnings.append(
                f"line {lineno}: (grandfathered) row has {arity} cells, "
                f"header declares {header_arity}; a literal | in cell prose must be escaped as \\|"
            )
    return OracleResult(warnings, errors)


def finding_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if FINDING_RE.match(line)]


def canonical_finding(line: str) -> str:
    match = FINDING_RE.match(line)
    if match is None:
        return line
    severity, label, message = match.groups()
    if message.startswith("INDEX.md: "):
        label = label.rstrip("/") + "/INDEX.md"
        message = message.removeprefix("INDEX.md: ")
    return f"{severity} {label}: {message}"


def capture_sections(path: Path) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "# --- section ---":
            sections.append([])
        else:
            sections[-1].append(line)
    return sections


def capture_exit(lines: list[str]) -> int | None:
    exits = [line.removeprefix("# exit: ") for line in lines if line.startswith("# exit: ")]
    if len(exits) != 1 or not exits[0].isdigit():
        return None
    return int(exits[0])


def capture_mode_path(lines: list[str]) -> tuple[str, str] | None:
    commands = [line.removeprefix("# cmd:").strip() for line in lines if line.startswith("# cmd:")]
    if len(commands) != 1:
        return None
    words = shlex.split(commands[0])
    if "--index" not in words:
        return None
    index = words.index("--index")
    if index + 1 >= len(words):
        return None
    path = Path(words[index + 1])
    label = f"{path.parent.name}/{path.name}"
    return ("audit" if "--index-audit" in words else "plain", label)


def oracle_sections(path: Path) -> list[tuple[str, str, int, list[str]]]:
    found: list[tuple[str, str, int, list[str]]] = []
    mode: str | None = None
    label: str | None = None
    expected_exit: int | None = None
    findings: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# oracle-section: "):
            if mode is not None and label is not None and expected_exit is not None:
                found.append((mode, label, expected_exit, findings))
            match = re.fullmatch(r"# oracle-section: mode=(plain|audit) path=(.+)", line)
            if match is None:
                raise ValueError("malformed oracle section")
            mode, label = match.groups()
            expected_exit = None
            findings = []
        elif line.startswith("# expected-exit: ") and mode is not None:
            expected_exit = int(line.removeprefix("# expected-exit: "))
        elif FINDING_RE.match(line) and mode is not None:
            findings.append(line)
    if mode is not None and label is not None and expected_exit is not None:
        found.append((mode, label, expected_exit, findings))
    return found


def emit(pairs: list[list[str]]) -> int:
    for mode, raw_path in pairs:
        path = Path(raw_path)
        result = oracle(path, mode == "audit")
        print(f"# oracle-section: mode={mode} path={path}")
        print(f"# expected-exit: {1 if result.errors else 0}")
        for warning in result.warnings:
            print(f"WARN {path}: {warning}")
        for error in result.errors:
            print(f"ERROR {path}: {error}")
    return 0


def compare(capture: Path, expected: Path) -> int:
    actual_sections = capture_sections(capture)
    expected_sections = oracle_sections(expected)
    if len(actual_sections) != len(expected_sections):
        return fail(
            f"section count mismatch: candidate={len(actual_sections)} oracle={len(expected_sections)}"
        )
    for number, (actual, expected_section) in enumerate(
        zip(actual_sections, expected_sections), 1
    ):
        expected_mode, expected_path, expected_status, expected_findings = expected_section
        actual_binding = capture_mode_path(actual)
        if actual_binding != (expected_mode, expected_path):
            return fail(
                f"section {number} mode/path mismatch: candidate={actual_binding!r} "
                f"oracle={(expected_mode, expected_path)!r}"
            )
        status = capture_exit(actual)
        if status != expected_status:
            return fail(
                f"section {number} exit mismatch: candidate={status} oracle={expected_status}"
            )
        if Counter(finding_lines(actual)) != Counter(expected_findings):
            return fail(f"section {number} finding multiset mismatch")
    print(f"index-oracle compare: PASS ({len(actual_sections)} sections)")
    return 0


def validated_findings(path: Path) -> list[str] | None:
    sections = capture_sections(path)
    if len(sections) != 1 or capture_exit(sections[0]) not in {0, 1}:
        return None
    return [canonical_finding(line) for line in finding_lines(sections[0])]


def leg_a(before_path: Path, after_path: Path, oracle_path: Path) -> int:
    before = validated_findings(before_path)
    after = validated_findings(after_path)
    if before is None or after is None:
        return fail("Leg A capture exit header is not exactly one value in {0,1}")
    sections = oracle_sections(oracle_path)
    if len(sections) != 1 or sections[0][0] != "plain":
        return fail("Leg A oracle is not exactly one plain-mode section")
    expected_added = Counter(canonical_finding(line) for line in sections[0][3])
    before_counter = Counter(before)
    after_counter = Counter(after)
    before_only = before_counter - after_counter
    after_only = after_counter - before_counter
    expected_removed = Counter(
        line
        for line in before
        if (match := FINDING_RE.match(line)) and Path(match.group(2)).name == "INDEX.md"
    )
    if before_only != expected_removed:
        return fail("Leg A before-only multiset is not exactly the baseline INDEX findings")
    if after_only != expected_added:
        return fail("Leg A after-only multiset is not exactly the plain oracle findings")
    if before_counter - expected_removed != after_counter - expected_added:
        return fail("Leg A non-INDEX remainder is not byte-identical with multiplicity")
    print(
        "LEG A PASS: "
        f"before={sum(before_counter.values())} after={sum(after_counter.values())} "
        f"removed={sum(before_only.values())} added={sum(after_only.values())}"
    )
    return 0


def leg_b(before_path: Path, after_path: Path) -> int:
    before = validated_findings(before_path)
    after = validated_findings(after_path)
    if before is None or after is None:
        return fail("Leg B capture exit header is not exactly one value in {0,1}")
    before_counter = Counter(before)
    after_counter = Counter(after)
    if before_counter - after_counter or after_counter - before_counter:
        return fail("Leg B signed finding multisets are not both empty")
    print(f"LEG B PASS: identical findings={sum(before_counter.values())}")
    return 0


def leg_d(path: Path) -> int:
    lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
    header_line = 0
    header_arity: int | None = None
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_cells(stripped)
        if len(cells) >= 2 and cells[0].lower() == "time":
            header_line = lineno
            header_arity = len(cells)
            break
    if header_arity is None:
        return fail("Leg D index has no data header")
    rows: list[dict[str, object]] = []
    for lineno, line in enumerate(lines, 1):
        if lineno <= header_line or not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line.strip())
        if cells and set(cells[0]) <= set("-: "):
            continue
        if len(cells) != header_arity:
            kind = "arity-error"
        elif has_escaped_pipe(line):
            kind = "escaped-valid"
        else:
            kind = "ordinary-valid"
        rows.append(
            {"line": lineno, "class": kind, "cells": cells, "raw": line}
        )
    counts = Counter(str(row["class"]) for row in rows)
    payload = {
        "path": str(path),
        "header_line": header_line,
        "header_arity": header_arity,
        "population": len(rows),
        "counts": dict(sorted(counts.items())),
        "rows": rows,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", nargs=2, action="append", metavar=("MODE", "PATH"))
    parser.add_argument("--compare", nargs=2, metavar=("CAPTURE", "ORACLE"))
    parser.add_argument(
        "--leg-a-delta", nargs=3, metavar=("BEFORE", "AFTER", "ORACLE")
    )
    parser.add_argument("--leg-b-identical", nargs=2, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--leg-d", type=Path, metavar="INDEX")
    args = parser.parse_args()
    chosen = sum(
        value is not None
        for value in (
            args.emit,
            args.compare,
            args.leg_a_delta,
            args.leg_b_identical,
            args.leg_d,
        )
    )
    if chosen != 1:
        parser.error("choose exactly one operation")
    try:
        if args.emit is not None:
            if any(mode not in {"plain", "audit"} for mode, _ in args.emit):
                return fail("--emit MODE must be plain or audit")
            return emit(args.emit)
        if args.compare is not None:
            return compare(Path(args.compare[0]), Path(args.compare[1]))
        if args.leg_a_delta is not None:
            return leg_a(*(Path(value) for value in args.leg_a_delta))
        if args.leg_b_identical is not None:
            return leg_b(*(Path(value) for value in args.leg_b_identical))
        return leg_d(args.leg_d)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
