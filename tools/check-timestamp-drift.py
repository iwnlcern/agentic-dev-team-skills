#!/usr/bin/env python3
"""Run the relay-lint timestamp-drift and index-monotonicity checks.

These checks are wall-clock dependent, so their fixtures are generated at run time
rather than committed with frozen stamps (a committed "fresh" stamp is stale by the
next run). Kept separate from check-relay-lint-fixtures.py, whose matrix is static
expected-exit-code pairs.

This is a dev/release helper. It imports relay-lint directly and writes only to a
temporary directory; it does not call any LLM, network, subprocess, or git command.
"""
from __future__ import annotations

import datetime
import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT = ROOT / "tools" / "relay-lint.py"

RELAY_BODY = """## DESIGN — timestamp check fixture

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: ts-check
PARENT_DISPATCH_ID: ts-check
RUN_ID: ts
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: master.orchestrator-planner
TO: master.orchestrator-reviewer
SUBJECT: timestamp check fixture

## Body
Fixture body.

ACTIONS_GIT_REF: none — fixture.
FINAL_GIT_STATUS_SHORT: unavailable — fixture.
"""

INDEX_HEADER = (
    "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)


def load_linter():
    spec = importlib.util.spec_from_file_location("relay_lint", LINT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {LINT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(dt: datetime.datetime) -> str:
    return dt.strftime("%Y%m%d-%H%M%S")


def index_row(time_cell: str, file_stamp: str) -> str:
    return (
        f"| {time_cell} | DESIGN | orchestrator-planner | d | p "
        f"| master.orchestrator-planner | operator | x | routed "
        f"| a/DESIGN-orchestrator-planner-{file_stamp}.md |\n"
    )


def main() -> int:
    lint = load_linter()
    now = datetime.datetime.now()
    fresh = stamp(now)
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def relay(name: str) -> Path:
            p = tmp / name
            p.write_text(RELAY_BODY, encoding="utf-8")
            return p

        # -- filename stamp: freshness is enforced only when asked for --------
        p = relay(f"DESIGN-orchestrator-planner-{fresh}.md")
        r = lint.lint_file(p, freshness=True)
        check("fresh stamp passes", r.ok, "; ".join(r.errors))

        drifted = stamp(now + datetime.timedelta(days=3, hours=9))
        p = relay(f"DESIGN-orchestrator-planner-{drifted}.md")
        r = lint.lint_file(p, freshness=True)
        check(
            "future drift rejected",
            not r.ok and any("in the future" in e for e in r.errors),
            "; ".join(r.errors),
        )

        stale = stamp(now - datetime.timedelta(hours=5))
        p = relay(f"DESIGN-orchestrator-planner-{stale}.md")
        r = lint.lint_file(p, freshness=True)
        check(
            "past drift rejected",
            not r.ok and any("in the past" in e for e in r.errors),
            "; ".join(r.errors),
        )
        r = lint.lint_file(p, freshness=False)
        check("historical relay passes without freshness", r.ok, "; ".join(r.errors))

        edge = stamp(now - datetime.timedelta(minutes=5))
        p = relay(f"DESIGN-orchestrator-planner-{edge}.md")
        check(
            "inside tolerance passes / outside fails",
            lint.lint_file(p, freshness=True, max_drift_minutes=15).ok
            and not lint.lint_file(p, freshness=True, max_drift_minutes=2).ok,
        )

        # An impossible time is a defect in every mode, freshness or not.
        p = relay("DESIGN-orchestrator-planner-20260712-016200.md")
        r = lint.lint_file(p, freshness=False)
        check(
            "impossible time rejected without freshness",
            not r.ok and any("not a real date/time" in e for e in r.errors),
            "; ".join(r.errors),
        )

        p = relay("DESIGN-orchestrator-planner-nostamp.md")
        check(
            "missing stamp: rejected when authoring, ignored otherwise",
            not lint.lint_file(p, freshness=True).ok and lint.lint_file(p, freshness=False).ok,
        )

        # -- index: monotonicity, boundary, filename agreement, freshness ----
        def index(rows: str, marker: str | None = None) -> Path:
            p = tmp / "INDEX.md"
            body = INDEX_HEADER + rows
            if marker is not None:
                body += f"\n<!-- relay-lint: monotonic-from {marker} -->\n"
            p.write_text(body, encoding="utf-8")
            return p

        good = index_row(fresh, fresh)
        r = lint.lint_relay_index(index(good), freshness=True)
        check("index: good row passes", r.ok, "; ".join(r.errors))

        back = stamp(now - datetime.timedelta(hours=2))
        r = lint.lint_relay_index(index(good + index_row(back, back)), freshness=True)
        check(
            "index: decreasing row rejected",
            not r.ok and any("non-decreasing" in e for e in r.errors),
            "; ".join(r.errors),
        )

        r = lint.lint_relay_index(index(index_row(fresh, drifted)), freshness=True)
        check(
            "index: row disagreeing with its filename rejected",
            not r.ok and any("disagrees with its filename" in e for e in r.errors),
            "; ".join(r.errors),
        )

        r = lint.lint_relay_index(index(index_row(drifted, drifted)), freshness=True)
        check(
            "index: fabricated future row rejected",
            not r.ok and any("in the future" in e for e in r.errors),
            "; ".join(r.errors),
        )

        r = lint.lint_relay_index(index(index_row("20260712-016200", fresh)), freshness=True)
        check(
            "index: impossible time rejected",
            not r.ok and any("not a valid timestamp" in e for e in r.errors),
            "; ".join(r.errors),
        )

        # The marker grandfathers everything above it and floors everything below.
        hist = index_row(stamp(now - datetime.timedelta(days=30)), stamp(now - datetime.timedelta(days=30)))
        hist += index_row(stamp(now - datetime.timedelta(days=31)), stamp(now - datetime.timedelta(days=31)))
        r = lint.lint_relay_index(index(hist, marker=fresh), freshness=False)
        check("index: pre-marker history grandfathered", r.ok, "; ".join(r.errors))

        r = lint.lint_relay_index(index(hist, marker=fresh), freshness=False, audit=True)
        check(
            "index: audit surfaces grandfathered history",
            any("grandfathered history" in w for w in r.warnings),
            "; ".join(r.warnings),
        )

        p = tmp / "INDEX.md"
        p.write_text(
            INDEX_HEADER + hist + f"\n<!-- relay-lint: monotonic-from {fresh} -->\n"
            + index_row(back, back),
            encoding="utf-8",
        )
        r = lint.lint_relay_index(p, freshness=False)
        check(
            "index: post-marker row below the boundary rejected",
            not r.ok and any("predates the monotonic-from boundary" in e for e in r.errors),
            "; ".join(r.errors),
        )

    width = max(len(n) for n, _o, _d in results)
    failed = False
    for name, ok, detail in results:
        failed = failed or not ok
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}")
        if not ok and detail:
            print(f"  {detail}")
    print(f"\n{sum(1 for _n, o, _d in results if o)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
