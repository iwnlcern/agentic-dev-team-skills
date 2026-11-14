#!/usr/bin/env python3
"""Release check runner (DD-v3-tests § 3.2).

Runs every test module and check of the tree, one process per step, across a
worker pool, under a wall cap per leg; anything unfinished at the cap counts as
failed.  The runner never turns a failure green.

Usage: python3 tools/release-check.py full --out DIR [--leg mac|linux|both]
           [--workers N] [--cap-seconds S] [--release]
The linux leg runs the same script inside a pinned container
(tools/release-check.Dockerfile) with the worktree and its common Git directory
mounted read-only at their host paths.
Exit: 0 all green, 1 any step failed or unfinished, 2 setup error.
"""

import argparse
import concurrent.futures
import dataclasses
import datetime
import errno
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKER = "ADT_RELEASE_STEP"
KILL_GRACE_SECONDS = 10.0
# A release check runs both legs, each under R265's 30 minutes (DD-v3-tests:42, :63).
RELEASE_CAP_SECONDS = 1800
STARTED_LOG = "started.jsonl"
POLL_SECONDS = 0.05
# SIGKILL passes over a step's leftovers before the runner stops trying (plan r4 L6).
SWEEP_ROUNDS = 5
# macOS hides these binaries' environment from a non-root process, so a marker cannot
# attribute them (plan r4 change 1); detached ones are listed, never killed.
PLATFORM_DIRS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/System/")
BOTH = frozenset({"mac", "linux"})
MAC = frozenset({"mac"})
# The one engine module that runs on the mac leg only: pdc's real inbox sweeper reads the
# engine's mirror headers against the operator's own checkout, which the linux image never
# has; a pdc break blocks the release unless the operator approves it by name
# (DD-v3-tests § 3.6, PR-3 G-E2).  Its skip on a host without pdc fails the step.
PDC_CONSUMER = "relay_engine.tests.test_pdc_consumer"
DOCKERFILE = Path(__file__).resolve().parent / "release-check.Dockerfile"
CONTAINER_OUT = "/out"
CONTAINER_SCRATCH = "/scratch"
CONTAINER_PLAN = "container-plan.json"
# Host-side wait beyond the container's own cap and kill grace before it is stopped.
CONTAINER_MARGIN_SECONDS = 120
# docker stop's timeout beyond the in-container grace, so the runner inside finishes its
# own grace and writes results before Docker's SIGKILL (plan r4 L4; review 4 B1 probe).
DOCKER_STOP_MARGIN_SECONDS = 20
# Each linux-leg container carries this label with the run's id (plan r4 T11).
RUN_LABEL = "adt.release-check.run"
CONTAINER_RECORD = "container.json"
# History the tests read from pinned commits must resolve in the container (DD AC 14):
# test_gate_review's PRE_B8_BASE.
PINNED_HISTORY = ("2138c1c",)
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
DOCKER_PLATFORMS = {"aarch64": "linux/arm64", "arm64": "linux/arm64",
                    "x86_64": "linux/amd64", "amd64": "linux/amd64"}
# Set when the run is interrupted; legs stop their steps (or container) and record it.
STOP = threading.Event()


class SetupError(Exception):
    """A leg could not start: exit 2."""


class Stopped(Exception):
    """The run was interrupted before the leg started its steps."""


def _request_stop(signum, frame):
    """SIGINT, SIGTERM and SIGHUP: stop once; later signals are ignored until results are written."""
    if not STOP.is_set():
        STOP.set()
        print(f"release-check: {signal.Signals(signum).name}: stopping the steps and writing results; "
              "further interrupts are ignored until then", file=sys.stderr, flush=True)


@dataclasses.dataclass(frozen=True)
class Step:
    id: str
    argv: list
    legs: frozenset
    covers: tuple
    # Passes only on a unittest summary of OK with tests run and none skipped (erratum 1 Q1).
    summary: bool = False


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


ADAPTER_SCRIPT = "tools/adapters/claude-code/test-adapter.sh"
ADAPTER_MODULES = re.compile(r"-m unittest((?:\s+tools\.adapters\.tests\.\w+)+)")


def adapter_modules(script):
    """The adapter test modules test-adapter.sh runs, read from its unittest line(s)."""
    names = set()
    for match in ADAPTER_MODULES.finditer(Path(script).read_text()):
        names.update(name.rsplit(".", 1)[1] for name in match.group(1).split())
    return sorted(names)


def step_table(repo):
    """Every step the tree offers, in run order: engine modules first, then the rest."""
    repo = Path(repo)
    python = sys.executable
    steps = []
    for path in sorted((repo / "tools/relay_engine/tests").glob("test_*.py")):
        module = "relay_engine.tests." + path.stem
        steps.append(Step(module, [python, "-m", "unittest", module],
                          MAC if module == PDC_CONSUMER else BOTH, (_rel(repo, path),),
                          summary=True))
    adapter = repo / ADAPTER_SCRIPT
    if adapter.is_file():
        # Covers exactly the modules test-adapter.sh names (plan r4 change 2); any other
        # adapter module is uncovered.  Its unittest block is held to the summary rule (T8).
        covers = (ADAPTER_SCRIPT,) + tuple(f"tools/adapters/tests/{name}.py"
                                           for name in adapter_modules(adapter))
        steps.append(Step("test-adapter.sh", ["bash", ADAPTER_SCRIPT], BOTH, covers, summary=True))
    if (repo / "tools/kit_tests").is_dir():
        steps.append(Step("kit_tests", [python, "-m", "unittest", "discover", "-s", "tools/kit_tests"],
                          BOTH, ("tools/kit_tests",), summary=True))
    if (repo / "tools/release_check_tests").is_dir():
        # Mac only: its Docker tests need the host's Docker.
        steps.append(Step("release_check_tests",
                          [python, "-m", "unittest", "discover", "-s", "tools/release_check_tests"],
                          MAC, ("tools/release_check_tests",), summary=True))
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
    """Every test_*.py under tools/ at any depth (plan r6 change 2), the checks, the battery,
    and the directory steps' directories."""
    repo = Path(repo)
    units = [_rel(repo, p) for p in sorted(repo.glob("tools/**/test_*.py"))
             if "__pycache__" not in p.relative_to(repo).parts]
    units.extend(_rel(repo, p) for p in sorted(repo.glob("tools/check-*.py")))
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
    # A directory step covers every file inside its directory.
    directories = tuple(c + "/" for c in covered if (repo / c).is_dir())
    problems = []
    for unit in _discovered_units(repo):
        if unit in covered or unit.startswith(directories):
            continue
        if not any(_matches(unit, e) for e in exclusions):
            problems.append(f"{unit}: discovered but neither a step nor an exclusion")
    for exclusion in exclusions:
        if not list(repo.glob(exclusion.path)):
            problems.append(f"{exclusion.path}: exclusion matches no file (stale entry)")
    return problems


def _listing():
    """(pid, pgid, markers) for this user's live processes; markers are the MARKER values
    found in each environment.  Read as bytes: any process's environment may hold bytes
    that are not UTF-8.  Zombies are skipped (they hold no environment and are not running)."""
    prefix = f"{MARKER}=".encode()
    procs = []
    if sys.platform.startswith("linux"):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                environ = Path(entry.path, "environ").read_bytes().split(b"\0")
                stat = Path(entry.path, "stat").read_bytes().rsplit(b")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if stat[0] == b"Z":
                continue
            markers = frozenset(e[len(prefix):] for e in environ if e.startswith(prefix))
            procs.append((int(entry.name), int(stat[2]), markers))
    else:
        listing = subprocess.run(
            ["ps", "-E", "-ww", "-o", "pid=,pgid=,stat=,command=", "-U", str(os.getuid())],
            capture_output=True, stdin=subprocess.DEVNULL, check=True).stdout
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) < 3 or not (fields[0].isdigit() and fields[1].isdigit()):
                continue
            if fields[2].startswith(b"Z"):
                continue
            markers = frozenset(f[len(prefix):] for f in fields[3:] if f.startswith(prefix))
            procs.append((int(fields[0]), int(fields[1]), markers))
    me = os.getpid()
    return [p for p in procs if p[0] != me]


def _signal(pid, sig, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


@dataclasses.dataclass
class _Sweep:
    """One step's processes: those carrying its marker or in its process group.

    SIGTERM on sight, SIGKILL from kill_at, up to SWEEP_ROUNDS passes; sweeps advance
    between scheduler polls, so a slow leftover never holds up the other steps.
    A step's pid is not reused while its process group has members, so matching the
    group stays safe for the sweep's life."""
    marker: bytes
    pgid: int
    row: dict
    exclude: int = None
    kill_at: float = None
    rounds: int = 0
    seen: set = dataclasses.field(default_factory=set)
    signalled: set = dataclasses.field(default_factory=set)

    def finish(self, survivors=()):
        self.row["leaked"] = sorted(self.seen - {self.exclude})
        if survivors:
            print(f"release-check: {self.row['id']}: {sorted(survivors)} survived "
                  f"{SWEEP_ROUNDS} SIGKILL rounds", file=sys.stderr)


def _advance_sweeps(sweeps):
    """One pass over the pending sweeps; finished ones leave the list."""
    listing = _listing()
    now = time.monotonic()
    for sweep in list(sweeps):
        alive = {pid for pid, group, markers in listing
                 if sweep.marker in markers or group == sweep.pgid}
        sweep.seen |= alive
        if not alive:
            sweep.finish()
            sweeps.remove(sweep)
            continue
        if sweep.kill_at is None:
            sweep.kill_at = now + KILL_GRACE_SECONDS
        if now < sweep.kill_at:
            for pid in alive - sweep.signalled:
                _signal(pid, signal.SIGTERM)
            sweep.signalled |= alive
        elif sweep.rounds < SWEEP_ROUNDS:
            for pid in alive:
                _signal(pid, signal.SIGKILL)
            sweep.rounds += 1
        else:
            sweep.finish(alive)
            sweeps.remove(sweep)


def _unattributed(since, directories):
    """mac: the user's parent-1 platform binaries started at or after `since` whose working
    directory is under one of `directories` (plan r4 change 1).  Listed, never killed."""
    if sys.platform.startswith("linux"):
        return []
    listing = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,lstart=,comm="],
                             capture_output=True, stdin=subprocess.DEVNULL, check=True,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    candidates = {}
    for line in listing.splitlines():
        fields = line.split(None, 7)
        if len(fields) < 8 or fields[1] != b"1":
            continue
        command = os.fsdecode(fields[7].strip())
        if not command.startswith(PLATFORM_DIRS):
            continue
        started = datetime.datetime.strptime(os.fsdecode(b" ".join(fields[2:7])), "%a %b %d %H:%M:%S %Y")
        if started >= since:
            candidates[int(fields[0])] = (command, started)
    if not candidates:
        return []
    lsof = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, candidates))],
                          capture_output=True, stdin=subprocess.DEVNULL).stdout
    cwds, pid = {}, None
    for line in lsof.splitlines():
        if line.startswith(b"p"):
            pid = int(line[1:])
        elif line.startswith(b"n") and pid is not None:
            cwds[pid] = os.fsdecode(line[1:])
    roots = [os.path.realpath(d) for d in directories]
    entries = []
    for pid, (command, started) in sorted(candidates.items()):
        cwd = cwds.get(pid)
        if cwd and any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
            entries.append({"pid": pid, "command": command, "cwd": cwd,
                            "started": started.isoformat(timespec="seconds")})
    return entries


def _unittest_reason(log_path):
    """None when the last unittest summary is OK with at least one test and no skips
    (AGENTS.md:18); the status read is the line after that summary's "Ran" line."""
    lines = Path(log_path).read_text(errors="replace").splitlines()
    ran = [i for i, line in enumerate(lines) if re.match(r"Ran \d+ tests? in ", line)]
    if not ran or int(lines[ran[-1]].split()[1]) == 0:
        return "no-tests"
    status = next((line for line in lines[ran[-1] + 1:] if line.strip()), "")
    if status.startswith("NO TESTS RAN"):
        return "no-tests"
    if status.startswith("OK") and "skipped=" in status:
        return "skipped"
    return None


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
    results_path: Path = None


def run_leg(leg, steps, workers, cap_seconds, out, *, repo=REPO, environ=None, exclusions=EXCLUSIONS,
            leg_dir=None):
    """Run one leg's steps under the cap; write out/<leg>/results.json and one log per step."""
    environ = dict(os.environ if environ is None else environ)
    repo = Path(repo)
    leg_out = Path(leg_dir) if leg_dir is not None else Path(out) / leg
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
        "machine": platform.machine(), "image": environ.get("ADT_IMAGE_ID"), "interrupted": False,
        "unattributed": [], "error": None,
        "started": _now(), "finished": None,
        "exclusions": [{"path": e.path, "reason": e.reason} for e in exclusions],
        "steps": [], "exit": None,
    }
    rows = {}
    for index, step in enumerate(active):
        log = logs / f"{index:03d}-{step.id.replace('/', '_').replace(' ', '_')}.log"
        rows[step.id] = {"id": step.id, "argv": list(step.argv), "outcome": "unfinished",
                         "reason": None, "exit_code": None, "wall_seconds": 0.0, "log": str(log), "leaked": []}

    since = datetime.datetime.now().replace(microsecond=0)
    deadline = time.monotonic() + cap_seconds
    pending = list(active)
    running = {}
    sweeps = []
    errors = []
    started_log = open(leg_out / STARTED_LOG, "a", encoding="utf-8")
    try:
        _schedule(pending, running, sweeps, rows, base_env, repo, workers, deadline,
                  {s.id for s in active if s.summary}, started_log)
    except KeyboardInterrupt:
        STOP.set()
    except Exception as error:  # noqa: BLE001 - any runner failure still stops and writes
        traceback.print_exc()
        errors.append(f"{type(error).__name__}: {error}")
    if STOP.is_set():
        results["interrupted"] = True
    _stop_all(running, sweeps, rows, errors)
    started_log.close()
    for step in pending:
        Path(rows[step.id]["log"]).touch()
    try:
        results["unattributed"] = _unattributed(since, (repo, leg_out))
    except Exception as error:  # noqa: BLE001
        errors.append(f"unattributed scan: {type(error).__name__}: {error}")
    results["error"] = "; ".join(errors) or None

    results["steps"] = [rows[s.id] for s in active]
    results["load"]["end"] = list(os.getloadavg())
    results["finished"] = _now()
    exit_code = 2 if errors else 0 if all(r["outcome"] == "pass" for r in results["steps"]) else 1
    results["exit"] = exit_code
    results_path = leg_out / "results.json"
    results_path.write_text(json.dumps(results, indent=2) + "\n")
    failed = [r["id"] for r in results["steps"] if r["outcome"] != "pass"]
    print(f"release-check: {leg} leg exit {exit_code}; {len(failed)} of {len(active)} steps not green"
          + (" (interrupted)" if results["interrupted"] else "")
          + (f" (runner error: {results['error']})" if errors else "")
          + (": " + ", ".join(failed) if failed else ""), file=sys.stderr)
    return LegResult(leg, exit_code, results_path)


def _schedule(pending, running, sweeps, rows, base_env, repo, workers, deadline, summary_ids, started_log):
    """Start and reap steps, and sweep finished steps' leftovers, until all are done,
    the cap passes or the run is stopped."""
    while (pending or running or sweeps) and not STOP.is_set():
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
            # Written as the step starts, so a runner killed outright leaves a record of what
            # to stop (plan r6 change 3; tools/TESTING.md "After a SIGKILL").
            started_log.write(json.dumps({"id": step.id, "pid": proc.pid, "marker": marker}) + "\n")
            started_log.flush()
        for step_id, (proc, marker, started, handle) in list(running.items()):
            code = proc.poll()
            if code is None:
                continue
            handle.close()
            row = rows[step_id]
            reason = None if code == 0 else f"exit {code}"
            if step_id in summary_ids:
                # The summary decides, so the verdict does not depend on the interpreter
                # (3.12+ exits 5 on "NO TESTS RAN", 3.11 exits 0).
                summary = _unittest_reason(row["log"])
                if code == 0 or (summary == "no-tests" and code == 5):
                    reason = summary or reason
            row.update(outcome="pass" if reason is None else "fail", reason=reason, exit_code=code,
                       wall_seconds=round(time.monotonic() - started, 3))
            sweeps.append(_Sweep(marker.encode(), proc.pid, row))
            del running[step_id]
        if sweeps:
            _advance_sweeps(sweeps)
        if time.monotonic() >= deadline:
            break
        if pending or running or sweeps:
            time.sleep(POLL_SECONDS)


def _stop_all(running, sweeps, rows, errors):
    """The cap's (and an interrupt's, and an error's) stop path: every running step's group
    and marked processes and every pending sweep, under one shared grace, then SIGKILL."""
    now = time.monotonic()
    for step_id, (proc, marker, _, _) in running.items():
        _signal(proc.pid, signal.SIGTERM, group=True)
        sweeps.append(_Sweep(marker.encode(), proc.pid, rows[step_id], exclude=proc.pid,
                             kill_at=now + KILL_GRACE_SECONDS))
    while sweeps:
        for proc, _, _, _ in running.values():
            proc.poll()
        try:
            _advance_sweeps(sweeps)
        except Exception as error:  # noqa: BLE001 - fall back to the groups the runner made
            traceback.print_exc()
            errors.append(f"stop path: {type(error).__name__}: {error}")
            for sweep in sweeps:
                _signal(sweep.pgid, signal.SIGKILL, group=True)
                sweep.finish()
            sweeps.clear()
        if sweeps:
            time.sleep(POLL_SECONDS)
    for step_id, (proc, _, started, handle) in running.items():
        _signal(proc.pid, signal.SIGKILL, group=True)
        proc.wait()
        handle.close()
        rows[step_id].update(exit_code=proc.returncode,
                             wall_seconds=round(time.monotonic() - started, 3))
    running.clear()


def common_git_dir(repo):
    """The repository's common Git directory, absolute (a linked worktree's .git names it)."""
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise SetupError(f"{repo} is not a Git repository: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def image_id(docker, tag):
    result = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", tag],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return result.stdout.strip() or None


def image_tag():
    return "adt-release-check:" + hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]


def ensure_image(docker, platform=None):
    """Build the linux leg's image for the native platform when its tag is absent or foreign."""
    tag = image_tag()
    platform = platform or native_platform(docker)
    try:
        present = subprocess.run([docker, "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}",
                                  tag], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if present.returncode == 0 and present.stdout.strip() == platform:
            return tag
        with open(DOCKERFILE, "rb") as dockerfile, tempfile.TemporaryFile() as errors:
            build = subprocess.Popen([docker, "build", "-q", "--platform", platform, "-t", tag, "-"],
                                     stdin=dockerfile, stdout=subprocess.DEVNULL, stderr=errors)
            while build.poll() is None:
                if STOP.is_set():
                    build.terminate()
                    build.wait()
                    raise Stopped(f"interrupted during docker build of {tag}")
                time.sleep(0.2)
            errors.seek(0)
            stderr = errors.read().decode(errors="replace")
    except OSError as error:
        raise SetupError(f"docker could not run ({docker}): {error}") from error
    if build.returncode != 0:
        raise SetupError(f"docker build of {tag} failed: {stderr.strip()[-800:]}")
    return tag


def native_platform(docker):
    """The host's native Docker platform, so the linux leg never runs under emulation."""
    try:
        arch = subprocess.run([docker, "info", "--format", "{{.Architecture}}"], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL).stdout.strip()
    except OSError as error:
        raise SetupError(f"docker could not run ({docker}): {error}") from error
    if arch not in DOCKER_PLATFORMS:
        raise SetupError(f"docker reports an unknown host architecture {arch!r}")
    return DOCKER_PLATFORMS[arch]


def docker_args(worktree, common_git_dir, out, image, expected_head, extra=(), platform=None,
                image_id=None):
    """Arguments after the docker binary for the linux leg's container."""
    worktree = Path(worktree)
    common = Path(common_git_dir)
    sources = [worktree]
    if common != worktree and worktree not in common.parents:
        sources.append(common)
    # --init: an init process, not the runner, is PID 1 and reaps orphans (erratum 1 E1).
    args = ["run", "--rm", "--init", "--user", f"{os.getuid()}:{os.getgid()}"]
    if platform:
        args += ["--platform", platform]
    for source in sources:
        # A linked worktree's .git names the common directory by absolute host path,
        # so both are mounted at their host paths, read-only.
        args += ["--mount", f"type=bind,src={source},dst={source},readonly"]
    args += ["--mount", f"type=bind,src={Path(out) / 'linux'},dst={CONTAINER_OUT}"]
    # Docker's tmpfs mount is noexec by default; tests run scripts they write (erratum 1 E2).
    args += ["--tmpfs", f"{CONTAINER_SCRATCH}:rw,exec,mode=1777"]
    for key, value in (("ADT_EXPECTED_HEAD", expected_head),
                       ("ADT_SOURCE_MOUNTS", os.pathsep.join(str(s) for s in sources)),
                       ("TMPDIR", CONTAINER_SCRATCH), ("HOME", CONTAINER_SCRATCH),
                       ("RELAY_ENGINE_ACL_DIR", "/var/tmp"),
                       ("ADT_IMAGE_ID", image_id or "")):
        args += ["-e", f"{key}={value}"]
    args += ["-w", str(worktree), image, "python3", "tools/release-check.py", "full", "--leg", "linux",
             "--in-container", "--out", CONTAINER_OUT, *extra]
    return args


def write_container_plan(leg_dir, steps, leg, exclusions, history=PINNED_HISTORY):
    """The host's step selection, which the container runs as given, and the pinned history
    its preflight resolves."""
    plan = {"step_ids": [s.id for s in steps if leg in s.legs and not _excluded(s, exclusions)],
            "exclusions": [{"path": e.path, "reason": e.reason} for e in exclusions],
            "history": list(history)}
    path = Path(leg_dir) / CONTAINER_PLAN
    path.write_text(json.dumps(plan, indent=2) + "\n")
    return path


def preflight(repo, environ, history=()):
    """In the container: problems that make the leg's results untrustworthy."""
    problems = []
    for source in filter(None, environ.get("ADT_SOURCE_MOUNTS", "").split(os.pathsep)):
        if not os.path.isdir(source):
            problems.append(f"source mount {source} does not exist")
            continue
        try:
            handle, probe = tempfile.mkstemp(dir=source, prefix=".release-check-write-probe-")
        except OSError as error:
            # Only a read-only file system is the refusal the leg needs (DD AC 14).
            if error.errno != errno.EROFS:
                problems.append(f"source mount {source} could not be probed: {error}")
            continue
        os.close(handle)
        os.unlink(probe)
        problems.append(f"source mount {source} accepts a write")
    expected = environ.get("ADT_EXPECTED_HEAD", "")
    head = _commit(repo)
    if not head or head != expected:
        problems.append(f"HEAD {head} in the container does not equal the host's {expected or '(unset)'}")
        return problems
    for ref in (f"{head}:AGENTS.md", *(f"{commit}:" for commit in history)):
        if subprocess.run(["git", "-C", str(repo), "show", ref], capture_output=True,
                          stdin=subprocess.DEVNULL).returncode != 0:
            problems.append(f"git show {ref} fails in the container")
    return problems


def _run_in_container(args, repo, environ):
    out = Path(args.out)
    plan = json.loads((out / CONTAINER_PLAN).read_text())
    problems = preflight(repo, environ, plan.get("history", ()))
    for problem in problems:
        print(f"preflight: {problem}", flush=True)
    if problems:
        return 2
    print("preflight: clean", flush=True)
    table = {s.id: s for s in step_table(repo)}
    missing = [step_id for step_id in plan["step_ids"] if step_id not in table]
    if missing:
        print(f"release-check: the container's tree lacks steps {missing}", flush=True)
        return 2
    exclusions = tuple(Exclusion(**e) for e in plan["exclusions"])
    return run_leg("linux", [table[i] for i in plan["step_ids"]], args.workers, args.cap_seconds, out,
                   repo=repo, environ=environ, exclusions=exclusions, leg_dir=out).exit


def _labelled(docker, label):
    listing = subprocess.run([docker, "ps", "-aq", "--filter", f"label={label}"], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL)
    return listing.stdout.split()


def _stop_container(docker, label, proc):
    """Stop this run's container so the runner inside writes its results: docker stop with a
    timeout beyond the in-container grace, retried until `docker run` exits (the container
    may not exist yet), then docker kill as the last resort."""
    timeout = int(KILL_GRACE_SECONDS) + DOCKER_STOP_MARGIN_SECONDS
    give_up = time.monotonic() + timeout + CONTAINER_MARGIN_SECONDS
    while proc.poll() is None:
        containers = _labelled(docker, label)
        if containers:
            subprocess.run([docker, "stop", "-t", str(timeout), *containers], capture_output=True,
                           stdin=subprocess.DEVNULL)
        if proc.poll() is None and time.monotonic() > give_up:
            subprocess.run([docker, "kill", *_labelled(docker, label)], capture_output=True,
                           stdin=subprocess.DEVNULL)
            proc.kill()
        time.sleep(0.2)
    proc.wait()
    leftover = _labelled(docker, label)
    if leftover:
        subprocess.run([docker, "rm", "-f", *leftover], capture_output=True, stdin=subprocess.DEVNULL)


def _run_linux_host(steps, args, repo, environ, exclusions, history=PINNED_HISTORY):
    leg_dir = Path(args.out) / "linux"
    leg_dir.mkdir(parents=True, exist_ok=True)
    results_path = leg_dir / "results.json"
    run_id = uuid.uuid4().hex
    name, label = f"adt-release-check-{run_id[:12]}", f"{RUN_LABEL}={run_id}"
    (leg_dir / CONTAINER_RECORD).write_text(json.dumps({"name": name, "label": label}) + "\n")
    try:
        docker = environ.get("DOCKER") or shutil.which("docker", path=environ.get("PATH"))
        if not docker or not os.access(docker, os.X_OK):
            raise SetupError(f"docker not found (DOCKER={environ.get('DOCKER')!r}, PATH lookup)")
        platform_name = native_platform(docker)
        image = ensure_image(docker, platform_name)
        command = docker_args(repo, common_git_dir(repo), args.out, image, _commit(repo) or "",
                              extra=["--workers", str(args.workers), "--cap-seconds", str(args.cap_seconds)],
                              platform=platform_name, image_id=image_id(docker, image))
        if STOP.is_set():
            raise Stopped("interrupted before the container started")
    except SetupError as error:
        print(f"release-check: linux leg setup error: {error}", file=sys.stderr)
        return LegResult("linux", 2)
    except Stopped as stopped:
        print(f"release-check: linux leg {stopped}", file=sys.stderr)
        return LegResult("linux", 1)
    write_container_plan(leg_dir, steps, "linux", exclusions, history)
    command = [docker, command[0], "--name", name, "--label", label] + command[1:]
    with open(leg_dir / "docker.log", "wb") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        give_up = time.monotonic() + args.cap_seconds + KILL_GRACE_SECONDS + CONTAINER_MARGIN_SECONDS
        while proc.poll() is None and not STOP.is_set() and time.monotonic() < give_up:
            time.sleep(0.2)
        overdue = proc.poll() is None and not STOP.is_set()
        # An interrupt that lands before the container exists stops a leg that ran nothing.
        before_start = proc.poll() is None and STOP.is_set() and not _labelled(docker, label)
        if proc.poll() is None:
            _stop_container(docker, label, proc)
    code = proc.returncode
    written = results_path.is_file()
    if before_start and not written:
        print(f"release-check: linux leg interrupted before its container started (exit {code})",
              file=sys.stderr)
        code = 1
    elif overdue:
        print("release-check: linux leg container outlived its own cap; stopped", file=sys.stderr)
        code = 2
    elif code not in (0, 1, 2):
        print(f"release-check: linux leg container exited {code}, not the runner's 0, 1 or 2",
              file=sys.stderr)
        code = 2
    elif code in (0, 1) and not written:
        print(f"release-check: linux leg exited {code} but wrote no {results_path}", file=sys.stderr)
        code = 2
    print(f"release-check: linux leg exit {code} (container log {leg_dir / 'docker.log'})", file=sys.stderr)
    return LegResult("linux", code, results_path if written else None)


def _under_home(path, environ):
    home = os.path.realpath(environ.get("HOME") or str(Path.home()))
    real = os.path.realpath(path)
    return real == home or real.startswith(home + os.sep)


def main(argv=None, *, repo=REPO, environ=None, exclusions=EXCLUSIONS, history=PINNED_HISTORY):
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
    full.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    STOP.clear()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous = {sig: signal.signal(sig, _request_stop) for sig in STOP_SIGNALS}
    try:
        if args.in_container:
            return _run_in_container(args, repo, environ)
        return _run_host(args, repo, environ, exclusions, history)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _guarded(leg, function, *args, **kwargs):
    """A leg that raises is a runner error: exit 2, with the traceback on stderr."""
    try:
        return function(*args, **kwargs)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print(f"release-check: {leg} leg runner error", file=sys.stderr)
        return LegResult(leg, 2)


def _run_host(args, repo, environ, exclusions, history):
    if not _under_home(args.out, environ):
        print(f"release-check: --out {args.out} is not under $HOME; macOS purges /tmp and "
              "/var/folders, so results there may vanish before they are cited", file=sys.stderr)
        return 2
    if args.release:
        refusals = [f"exclusion {e.path} ({e.reason})" for e in exclusions]
        if args.leg != "both":
            refusals.append(f"--leg {args.leg}: a release check runs both legs")
        if args.cap_seconds > RELEASE_CAP_SECONDS:
            refusals.append(f"--cap-seconds {args.cap_seconds}: a release leg is capped at "
                            f"{RELEASE_CAP_SECONDS} s (R265)")
        for refusal in refusals:
            print(f"release-check: --release refuses {refusal}", file=sys.stderr)
        if refusals:
            return 2
    if os.path.lexists(args.out) and not os.path.isdir(args.out):
        print(f"release-check: --out {args.out} exists and is not a directory", file=sys.stderr)
        return 2
    used = [p for p in ("release-check.json", "mac", "linux") if os.path.lexists(Path(args.out) / p)]
    if used:
        print(f"release-check: --out {args.out} already holds results ({', '.join(used)}); "
              "choose a new directory so no stale result is cited", file=sys.stderr)
        return 2
    steps = step_table(repo)
    problems = uncovered(repo, steps, exclusions)
    if problems:
        for problem in problems:
            print(f"release-check: refusing to start: {problem}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    legs = ("mac", "linux") if args.leg == "both" else (args.leg,)
    legs_run = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        linux = (pool.submit(_guarded, "linux", _run_linux_host, steps, args, repo, environ, exclusions,
                             history) if "linux" in legs else None)
        if "mac" in legs:
            legs_run["mac"] = _guarded("mac", run_leg, "mac", steps, args.workers, args.cap_seconds, out,
                                       repo=repo, environ=environ, exclusions=exclusions)
        if linux is not None:
            # Polled, so the main thread keeps taking signals while the linux leg runs.
            while not linux.done():
                time.sleep(0.2)
            legs_run["linux"] = linux.result()
    exits = {leg: result.exit for leg, result in legs_run.items()}
    overall = 2 if 2 in exits.values() else 1 if 1 in exits.values() else 0
    summary = {"commit": _commit(repo), "finished": _now(), "exit": overall,
               "release": args.release, "cap_seconds": args.cap_seconds, "interrupted": STOP.is_set(),
               "legs": {leg: {"exit": result.exit,
                              "results": str(result.results_path) if result.results_path else None}
                        for leg, result in legs_run.items()}}
    (out / "release-check.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"release-check: overall exit {overall} ({out / 'release-check.json'})", file=sys.stderr)
    return overall


if __name__ == "__main__":
    sys.exit(main())
