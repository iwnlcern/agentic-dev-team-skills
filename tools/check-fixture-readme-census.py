#!/usr/bin/env python3
"""Guard: the fixture README's family census must match the harness.

Runs check-relay-lint-fixtures.py, counts harness-asserted outcomes per
fixture family, and compares against the census table in
tools/relay-lint-fixtures/README.md. Any family the harness exercises that
the README omits, any family the README claims that the harness does not
exercise, and any per-family or total count mismatch is a failure.

This exists because the README's hand-maintained outcome table twice fell
behind the corpus while remaining row-accurate; completeness is checked
against the harness itself, never against the table's own rows.
"""

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "tools" / "relay-lint-fixtures" / "README.md"
HARNESS = ROOT / "tools" / "check-relay-lint-fixtures.py"

OUTCOME_RE = re.compile(r"^(?:--[a-z-]+ )?([A-Za-z0-9_-]+)/\S*: expected=")
CENSUS_ROW_RE = re.compile(r"^\| `([A-Za-z0-9_-]+)/` \| (\d+) \|")
TOTAL_RE = re.compile(r"^\*\*(\d+) harness-asserted outcomes\*\*")


def harness_census():
    proc = subprocess.run(
        [sys.executable, str(HARNESS)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode != 0:
        print("FAIL: check-relay-lint-fixtures.py itself failed "
              f"(exit {proc.returncode}); fix that first")
        sys.exit(1)
    counts = Counter()
    for line in proc.stdout.splitlines():
        m = OUTCOME_RE.match(line)
        if m:
            counts[m.group(1)] += 1
    if not counts:
        print("FAIL: parsed zero outcome lines from the harness; "
              "the output format changed — update OUTCOME_RE")
        sys.exit(1)
    return counts


def readme_census():
    counts = {}
    total = None
    for line in README.read_text().splitlines():
        m = CENSUS_ROW_RE.match(line)
        if m:
            family, n = m.group(1), int(m.group(2))
            if family in counts:
                print(f"FAIL: README census lists `{family}/` twice")
                sys.exit(1)
            counts[family] = n
        m = TOTAL_RE.match(line)
        if m:
            total = int(m.group(1))
    if not counts or total is None:
        print("FAIL: could not parse a census table and total from "
              f"{README.relative_to(ROOT)}")
        sys.exit(1)
    return counts, total


def main():
    actual = harness_census()
    documented, documented_total = readme_census()
    failures = []
    for family in sorted(set(actual) - set(documented)):
        failures.append(f"family `{family}/` ({actual[family]} outcomes) "
                        "is missing from the README census")
    for family in sorted(set(documented) - set(actual)):
        failures.append(f"README census lists `{family}/` but the harness "
                        "asserts no outcomes for it")
    for family in sorted(set(actual) & set(documented)):
        if actual[family] != documented[family]:
            failures.append(f"`{family}/`: README says {documented[family]}, "
                            f"harness asserts {actual[family]}")
    actual_total = sum(actual.values())
    if documented_total != actual_total:
        failures.append(f"total: README says {documented_total}, "
                        f"harness asserts {actual_total}")
    if failures:
        print(f"FAIL: fixture README census out of date "
              f"({len(failures)} mismatches):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"fixture-readme-census-ok families={len(actual)} "
          f"outcomes={actual_total}")


if __name__ == "__main__":
    main()
