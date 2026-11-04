#!/usr/bin/env python3
"""Release check runner (DD-v3-tests § 3.2).

Runs every test module and check of the tree, one process per step, across a
worker pool, under a wall cap per leg; anything unfinished at the cap counts as
failed.  The runner never turns a failure green.

Usage: python3 tools/release-check.py full --out DIR [--leg mac|linux|both]
           [--workers N] [--cap-seconds S] [--release]
Exit: 0 all green, 1 any step failed or unfinished, 2 setup error.
"""

import argparse
import dataclasses
import datetime
import fnmatch
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKER = "ADT_RELEASE_STEP"
KILL_GRACE_SECONDS = 10.0
POLL_SECONDS = 0.05
BOTH = frozenset({"mac", "linux"})
MAC = frozenset({"mac"})


@dataclasses.dataclass(frozen=True)
class Step:
    id: str
    argv: list
    legs: frozenset
    covers: tuple


@dataclasses.dataclass(frozen=True)
class Exclusion:
    path: str
    reason: str


# The single place a discovered unit is not run; each entry names its R250.5
# basis and the DD-v3-tests stage that removes the file.  A stale entry (its
# path matches nothing) stops the runner, and --release refuses a non-empty table.
EXCLUSIONS = (
    Exclusion("tools/check-index-oracle.py",
              "R250.5: argument-driven v2.9 oracle called by nothing (audit Q1); removed at DD stage 4"),
    Exclusion("tools/check-t11-closure.py",
              "R250.5: argument-driven v2.9 oracle called by nothing (audit Q1); removed at DD stage 4"),
    Exclusion("tools/check-dispatch-head.py",
              "R250.5: argument-driven v2.9 oracle called by nothing (audit Q1); removed at DD stage 4"),
    Exclusion("tools/check-s375-escaped-rows.py",
              "R250.5: argument-driven v2.9 oracle called by nothing (audit Q1); removed at DD stage 4"),
    Exclusion("tools/mtbattery.py",
              "R250.5: mutation battery retired once the live matrix cases move (operator O5); removed at DD stage 4"),
    Exclusion("tools/relay_engine/tests/test_xroot*.py",
              "R250.5: cross-root reader replaced by brief 5.9 (operator O1); removed with its code at DD stage 3; "
              "excluded so a leg's cap measures surviving code"),
    Exclusion("tools/relay_engine/tests/test_b9_fold*.py",
              "R250.5: cross-root reader replaced by brief 5.9 (operator O1); removed with its code at DD stage 3; "
              "excluded so a leg's cap measures surviving code"),
    Exclusion("tools/relay_engine/tests/test_authority_b9*.py",
              "R250.5: cross-root reader replaced by brief 5.9 (operator O1); removed with its code at DD stage 3; "
              "excluded so a leg's cap measures surviving code"),
)

# Checks the runner knows how to run, while their files exist (DD § 3.2 table).
KNOWN_CHECKS = (
    ("tools/generate-plugins.py", ("--check",), BOTH),
    # Validates packaging bytes, which do not vary by OS: mac only (R250.5, DD § 3.2).
    ("tools/check-generate-plugins.py", (), MAC),
    ("tools/check-relay-lint-fixtures.py", (), BOTH),
    ("tools/check-timestamp-drift.py", (), BOTH),
    ("tools/check-fixture-readme-census.py", (), BOTH),
)


def _rel(repo, path):
    return path.relative_to(repo).as_posix()


def step_table(repo):
    """Every step the tree offers, in run order: engine modules first, then the rest."""
    repo = Path(repo)
    python = sys.executable
    steps = []
    for path in sorted((repo / "tools/relay_engine/tests").glob("test_*.py")):
        module = "relay_engine.tests." + path.stem
        steps.append(Step(module, [python, "-m", "unittest", module], BOTH, (_rel(repo, path),)))
    adapter = repo / "tools/adapters/claude-code/test-adapter.sh"
    if adapter.is_file():
        # test-adapter.sh runs the adapter unittest modules itself (test-adapter.sh:887).
        covers = (_rel(repo, adapter),) + tuple(
            _rel(repo, p) for p in sorted((repo / "tools/adapters/tests").glob("test_*.py")))
        steps.append(Step("test-adapter.sh", ["bash", _rel(repo, adapter)], BOTH, covers))
    if (repo / "tools/kit_tests").is_dir():
        steps.append(Step("kit_tests", [python, "-m", "unittest", "discover", "-s", "tools/kit_tests"],
                          BOTH, ("tools/kit_tests",)))
    if (repo / "tools/release_check_tests").is_dir():
        # Mac only: its Docker tests need the host's Docker.
        steps.append(Step("release_check_tests",
                          [python, "-m", "unittest", "discover", "-s", "tools/release_check_tests"],
                          MAC, ("tools/release_check_tests",)))
    for relative, args, legs in KNOWN_CHECKS:
        if (repo / relative).is_file():
            step_id = " ".join([Path(relative).name, *args])
            steps.append(Step(step_id, [python, relative, *args], legs, (relative,)))
    return steps


def _matches(path, exclusion):
    return fnmatch.fnmatchcase(path, exclusion.path)


def _excluded(step, exclusions):
    return bool(step.covers) and all(any(_matches(c, e) for e in exclusions) for c in step.covers)


def _discovered_units(repo):
    repo = Path(repo)
    units = []
    for pattern in ("tools/relay_engine/tests/test_*.py", "tools/adapters/tests/test_*.py",
                    "tools/check-*.py"):
        units.extend(_rel(repo, p) for p in sorted(repo.glob(pattern)))
    if (repo / "tools/mtbattery.py").is_file():
        units.append("tools/mtbattery.py")
    for directory in ("tools/kit_tests", "tools/release_check_tests"):
        if (repo / directory).is_dir():
            units.append(directory)
    return units


def uncovered(repo, steps, exclusions):
    """Discovered units no step or exclusion covers, plus exclusions that match nothing."""
    repo = Path(repo)
    covered = {c for s in steps if not _excluded(s, exclusions) for c in s.covers}
    problems = []
    for unit in _discovered_units(repo):
        if unit not in covered and not any(_matches(unit, e) for e in exclusions):
            problems.append(f"{unit}: discovered but neither a step nor an exclusion")
    for exclusion in exclusions:
        if not list(repo.glob(exclusion.path)):
            problems.append(f"{exclusion.path}: exclusion matches no file (stale entry)")
    return problems


def _marked_pids(marker):
    """Pids of this user's processes whose environment carries MARKER=marker."""
    needle = f"{MARKER}={marker}"
    found = set()
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environ = (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if needle.encode() in environ:
                found.add(int(entry.name))
    else:
        listing = subprocess.run(
            ["ps", "-E", "-ww", "-o", "pid=,command=", "-U", str(os.getuid())],
            capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        for line in listing.splitlines():
            fields = line.split()
            if fields and fields[0].isdigit() and needle in fields[1:]:
                found.add(int(fields[0]))
    found.discard(os.getpid())
    return sorted(found)


def _signal(pid, sig, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_marked(marker, grace=KILL_GRACE_SECONDS):
    """SIGTERM every marked process, SIGKILL what is left after the grace; return the pids found."""
    pids = _marked_pids(marker)
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    remaining = pids
    while remaining and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        remaining = _marked_pids(marker)
    for pid in remaining:
        _signal(pid, signal.SIGKILL)
    return pids


def _pin_validator(environ):
    """Resolve CLAUDE_BIN, else the first claude on PATH, to its real (versioned) path."""
    candidate = environ.get("CLAUDE_BIN") or shutil.which("claude", path=environ.get("PATH"))
    if not candidate:
        return None, None
    real = os.path.realpath(candidate)
    try:
        version = subprocess.run([real, "--version"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        version = None
    return real, version


def _commit(repo):
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else None


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass
class LegResult:
    leg: str
    exit: int
    results_path: Path


def run_leg(leg, steps, workers, cap_seconds, out, *, repo=REPO, environ=None, exclusions=EXCLUSIONS):
    """Run one leg's steps under the cap; write out/<leg>/results.json and one log per step."""
    environ = dict(os.environ if environ is None else environ)
    repo = Path(repo)
    leg_out = Path(out) / leg
    logs = leg_out / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    engine_results = leg_out / "engine-results"
    engine_results.mkdir(exist_ok=True)
    active = [s for s in steps if leg in s.legs and not _excluded(s, exclusions)]

    base_env = dict(environ)
    base_env.update({
        "PYTHONPATH": str(repo / "tools"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONWARNINGS": "error::ResourceWarning",
        "RELAY_ENGINE_RESULTS_ROOT": str(engine_results),
        "RELAY_ENGINE_MATRIX_RUN": str(uuid.uuid4()),
    })
    claude_bin = claude_version = None
    if leg == "mac":
        claude_bin, claude_version = _pin_validator(environ)
        if claude_bin:
            base_env["CLAUDE_BIN"] = claude_bin

    results = {
        "leg": leg, "commit": _commit(repo), "os": platform.platform(),
        "python": {"path": sys.executable, "version": platform.python_version()},
        "workers": workers, "load": {"start": list(os.getloadavg()), "end": None},
        "claude_bin": claude_bin, "claude_version": claude_version, "cap_seconds": cap_seconds,
        "started": _now(), "finished": None,
        "exclusions": [{"path": e.path, "reason": e.reason} for e in exclusions],
        "steps": [], "exit": None,
    }
    rows = {}
    for index, step in enumerate(active):
        log = logs / f"{index:03d}-{step.id.replace('/', '_').replace(' ', '_')}.log"
        rows[step.id] = {"id": step.id, "argv": list(step.argv), "outcome": "unfinished",
                         "exit_code": None, "wall_seconds": 0.0, "log": str(log), "leaked": []}

    deadline = time.monotonic() + cap_seconds
    pending = list(active)
    running = {}
    while pending or running:
        while pending and len(running) < workers and time.monotonic() < deadline:
            step = pending.pop(0)
            marker = uuid.uuid4().hex
            env = dict(base_env)
            env[MARKER] = marker
            handle = open(rows[step.id]["log"], "wb")
            proc = subprocess.Popen(step.argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
                                    start_new_session=True)
            running[step.id] = (proc, marker, time.monotonic(), handle)
        for step_id, (proc, marker, started, handle) in list(running.items()):
            code = proc.poll()
            if code is None:
                continue
            handle.close()
            row = rows[step_id]
            row.update(outcome="pass" if code == 0 else "fail", exit_code=code,
                       wall_seconds=round(time.monotonic() - started, 3))
            row["leaked"] = _stop_marked(marker)
            del running[step_id]
        if time.monotonic() >= deadline:
            break
        if pending or running:
            time.sleep(POLL_SECONDS)

    if running:
        for proc, marker, _, _ in running.values():
            _signal(proc.pid, signal.SIGTERM, group=True)
            for pid in _marked_pids(marker):
                _signal(pid, signal.SIGTERM)
        grace_end = time.monotonic() + KILL_GRACE_SECONDS
        while time.monotonic() < grace_end and (
                any(p.poll() is None for p, _, _, _ in running.values())
                or any(_marked_pids(m) for _, m, _, _ in running.values())):
            time.sleep(POLL_SECONDS)
        for step_id, (proc, marker, started, handle) in running.items():
            _signal(proc.pid, signal.SIGKILL, group=True)
            for pid in _marked_pids(marker):
                _signal(pid, signal.SIGKILL)
            proc.wait()
            handle.close()
            rows[step_id].update(exit_code=proc.returncode,
                                 wall_seconds=round(time.monotonic() - started, 3))
    for step in pending:
        Path(rows[step.id]["log"]).touch()

    results["steps"] = [rows[s.id] for s in active]
    results["load"]["end"] = list(os.getloadavg())
    results["finished"] = _now()
    exit_code = 0 if all(r["outcome"] == "pass" for r in results["steps"]) else 1
    results["exit"] = exit_code
    results_path = leg_out / "results.json"
    results_path.write_text(json.dumps(results, indent=2) + "\n")
    failed = [r["id"] for r in results["steps"] if r["outcome"] != "pass"]
    print(f"release-check: {leg} leg exit {exit_code}; {len(failed)} of {len(active)} steps not green"
          + (": " + ", ".join(failed) if failed else ""), file=sys.stderr)
    return LegResult(leg, exit_code, results_path)


def _under_home(path, environ):
    home = os.path.realpath(environ.get("HOME") or str(Path.home()))
    real = os.path.realpath(path)
    return real == home or real.startswith(home + os.sep)


def main(argv=None, *, repo=REPO, environ=None, exclusions=EXCLUSIONS):
    environ = dict(os.environ if environ is None else environ)
    parser = argparse.ArgumentParser(prog="release-check.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    full = commands.add_parser("full", help="run every step on the chosen legs")
    full.add_argument("--out", required=True, help="results directory; must be under $HOME")
    full.add_argument("--leg", choices=("mac", "linux", "both"), default="both")
    full.add_argument("--workers", type=int, default=8)
    full.add_argument("--cap-seconds", type=int, default=1800)
    full.add_argument("--release", action="store_true",
                      help="release-head run: refuse while any exclusion remains")
    args = parser.parse_args(argv)

    if not _under_home(args.out, environ):
        print(f"release-check: --out {args.out} is not under $HOME; macOS purges /tmp and "
              "/var/folders, so results there may vanish before they are cited", file=sys.stderr)
        return 2
    if args.release and exclusions:
        for exclusion in exclusions:
            print(f"release-check: --release refuses exclusion {exclusion.path} ({exclusion.reason})",
                  file=sys.stderr)
        return 2
    steps = step_table(repo)
    problems = uncovered(repo, steps, exclusions)
    if problems:
        for problem in problems:
            print(f"release-check: refusing to start: {problem}", file=sys.stderr)
        return 2
    if args.leg != "mac":
        print("release-check: the linux leg is not available in this revision", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    return run_leg("mac", steps, args.workers, args.cap_seconds, out, repo=repo, environ=environ,
                   exclusions=exclusions).exit


if __name__ == "__main__":
    sys.exit(main())
