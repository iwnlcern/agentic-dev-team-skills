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
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKER = "ADT_RELEASE_STEP"
KILL_GRACE_SECONDS = 10.0
POLL_SECONDS = 0.05
BOTH = frozenset({"mac", "linux"})
MAC = frozenset({"mac"})
DOCKERFILE = Path(__file__).resolve().parent / "release-check.Dockerfile"
CONTAINER_OUT = "/out"
CONTAINER_SCRATCH = "/scratch"
CONTAINER_PLAN = "container-plan.json"
# Host-side wait beyond the container's own cap and kill grace before it is killed.
CONTAINER_MARGIN_SECONDS = 120


class SetupError(Exception):
    """A leg could not start: exit 2."""


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


def common_git_dir(repo):
    """The repository's common Git directory, absolute (a linked worktree's .git names it)."""
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise SetupError(f"{repo} is not a Git repository: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def image_tag():
    return "adt-release-check:" + hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]


def ensure_image(docker):
    """Build the linux leg's image when its tag (from the Dockerfile's digest) is absent."""
    tag = image_tag()
    try:
        present = subprocess.run([docker, "image", "inspect", tag], capture_output=True,
                                 stdin=subprocess.DEVNULL).returncode == 0
        if present:
            return tag
        with open(DOCKERFILE, "rb") as dockerfile:
            built = subprocess.run([docker, "build", "-q", "-t", tag, "-"], stdin=dockerfile,
                                   capture_output=True, text=True)
    except OSError as error:
        raise SetupError(f"docker could not run ({docker}): {error}") from error
    if built.returncode != 0:
        raise SetupError(f"docker build of {tag} failed: {built.stderr.strip()[-800:]}")
    return tag


def docker_args(worktree, common_git_dir, out, image, expected_head, extra=()):
    """Arguments after the docker binary for the linux leg's container."""
    worktree = Path(worktree)
    common = Path(common_git_dir)
    sources = [worktree]
    if common != worktree and worktree not in common.parents:
        sources.append(common)
    args = ["run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
    for source in sources:
        # A linked worktree's .git names the common directory by absolute host path,
        # so both are mounted at their host paths, read-only.
        args += ["--mount", f"type=bind,src={source},dst={source},readonly"]
    args += ["--mount", f"type=bind,src={Path(out) / 'linux'},dst={CONTAINER_OUT}"]
    args += ["--mount", f"type=tmpfs,dst={CONTAINER_SCRATCH},tmpfs-mode=1777"]
    for key, value in (("ADT_EXPECTED_HEAD", expected_head),
                       ("ADT_SOURCE_MOUNTS", os.pathsep.join(str(s) for s in sources)),
                       ("TMPDIR", CONTAINER_SCRATCH), ("HOME", CONTAINER_SCRATCH)):
        args += ["-e", f"{key}={value}"]
    args += ["-w", str(worktree), image, "python3", "tools/release-check.py", "full", "--leg", "linux",
             "--in-container", "--out", CONTAINER_OUT, *extra]
    return args


def write_container_plan(leg_dir, steps, leg, exclusions):
    """The host's step selection, which the container runs as given."""
    plan = {"step_ids": [s.id for s in steps if leg in s.legs and not _excluded(s, exclusions)],
            "exclusions": [{"path": e.path, "reason": e.reason} for e in exclusions]}
    path = Path(leg_dir) / CONTAINER_PLAN
    path.write_text(json.dumps(plan, indent=2) + "\n")
    return path


def preflight(repo, environ):
    """In the container: problems that make the leg's results untrustworthy."""
    problems = []
    for source in filter(None, environ.get("ADT_SOURCE_MOUNTS", "").split(os.pathsep)):
        try:
            handle, probe = tempfile.mkstemp(dir=source, prefix=".release-check-write-probe-")
        except OSError:
            continue
        os.close(handle)
        os.unlink(probe)
        problems.append(f"source mount {source} accepts a write")
    expected = environ.get("ADT_EXPECTED_HEAD", "")
    head = _commit(repo)
    if not head or head != expected:
        problems.append(f"HEAD {head} in the container does not equal the host's {expected or '(unset)'}")
    elif subprocess.run(["git", "-C", str(repo), "show", f"{head}:AGENTS.md"], capture_output=True,
                        stdin=subprocess.DEVNULL).returncode != 0:
        problems.append(f"git show {head}:AGENTS.md fails in the container")
    return problems


def _run_in_container(args, repo, environ):
    out = Path(args.out)
    problems = preflight(repo, environ)
    for problem in problems:
        print(f"preflight: {problem}", flush=True)
    if problems:
        return 2
    print("preflight: clean", flush=True)
    plan = json.loads((out / CONTAINER_PLAN).read_text())
    table = {s.id: s for s in step_table(repo)}
    missing = [step_id for step_id in plan["step_ids"] if step_id not in table]
    if missing:
        print(f"release-check: the container's tree lacks steps {missing}", flush=True)
        return 2
    exclusions = tuple(Exclusion(**e) for e in plan["exclusions"])
    return run_leg("linux", [table[i] for i in plan["step_ids"]], args.workers, args.cap_seconds, out,
                   repo=repo, environ=environ, exclusions=exclusions, leg_dir=out).exit


def _run_linux_host(steps, args, repo, environ, exclusions):
    leg_dir = Path(args.out) / "linux"
    leg_dir.mkdir(parents=True, exist_ok=True)
    try:
        docker = environ.get("DOCKER") or shutil.which("docker", path=environ.get("PATH"))
        if not docker or not os.access(docker, os.X_OK):
            raise SetupError(f"docker not found (DOCKER={environ.get('DOCKER')!r}, PATH lookup)")
        image = ensure_image(docker)
        command = docker_args(repo, common_git_dir(repo), args.out, image, _commit(repo) or "",
                              extra=["--workers", str(args.workers), "--cap-seconds", str(args.cap_seconds)])
    except SetupError as error:
        print(f"release-check: linux leg setup error: {error}", file=sys.stderr)
        return 2
    write_container_plan(leg_dir, steps, "linux", exclusions)
    name = "adt-release-check-" + uuid.uuid4().hex[:12]
    command = [docker, command[0], "--name", name] + command[1:]
    with open(leg_dir / "docker.log", "wb") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=args.cap_seconds + KILL_GRACE_SECONDS + CONTAINER_MARGIN_SECONDS)
        except subprocess.TimeoutExpired:
            subprocess.run([docker, "kill", name], capture_output=True, stdin=subprocess.DEVNULL)
            proc.wait()
            code = 1
    if code not in (0, 1, 2):
        code = 2
    print(f"release-check: linux leg exit {code} (container log {leg_dir / 'docker.log'})", file=sys.stderr)
    return code


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
    full.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.in_container:
        return _run_in_container(args, repo, environ)
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
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    legs = ("mac", "linux") if args.leg == "both" else (args.leg,)
    exits = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        linux = (pool.submit(_run_linux_host, steps, args, repo, environ, exclusions)
                 if "linux" in legs else None)
        if "mac" in legs:
            exits["mac"] = run_leg("mac", steps, args.workers, args.cap_seconds, out, repo=repo,
                                   environ=environ, exclusions=exclusions).exit
        if linux is not None:
            exits["linux"] = linux.result()
    overall = 2 if 2 in exits.values() else 1 if 1 in exits.values() else 0
    summary = {"commit": _commit(repo), "finished": _now(), "exit": overall,
               "legs": {leg: {"exit": code, "results": str(out / leg / "results.json")}
                        for leg, code in exits.items()}}
    (out / "release-check.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"release-check: overall exit {overall} ({out / 'release-check.json'})", file=sys.stderr)
    return overall


if __name__ == "__main__":
    sys.exit(main())
