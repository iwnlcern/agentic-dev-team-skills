import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

from relay_engine import cli, errors, strings
from relay_engine.tests.test_identity_matrix import cid_did


SOURCE_ROOT = Path(__file__).parents[3]
PYTHON = os.environ.get("PYTHON", os.sys.executable)
CID, DID = cid_did()
ALLOWED_SITE_FIELDS = (
    "factory=strings._make_emitter",
    "verbatim_site=cli.cmd_show",
    "binding_site=daemon.start",
    "diagnostic_write_site=daemon._log_diagnostic",
    "report_sites=cli.main,daemon._writer_loop",
)
ESCALATION = (
    "Hand-relay this escalation to the eligible starter: master-planner if "
    "a master tier exists (never the orchestrator session), else "
    "orchestrator-planner, else the operator. This escalation is the sole "
    "exception to daemon admission."
)


def _subparsers(parser):
    return next(action for action in parser._actions
                if isinstance(action, argparse._SubParsersAction))


def help_population(parser):
    found = [("<root>", parser.format_help())]

    def visit(current, prefix):
        action = next((candidate for candidate in current._actions
                       if isinstance(candidate,
                                     argparse._SubParsersAction)), None)
        if action is None:
            return
        for name, child in sorted(action.choices.items()):
            command = prefix + (name,)
            found.append((" ".join(command), child.format_help()))
            if any(isinstance(action, argparse._SubParsersAction)
                   for action in child._actions):
                visit(child, command)

    visit(parser, ())
    return found


def ruled_manifest():
    entries = []
    relay = SOURCE_ROOT / "tools/relay"
    entries.append((relay, "relay"))
    for source in sorted((SOURCE_ROOT / "tools/relay_engine").glob("*.py")):
        entries.append((source, "relay_engine/" + source.name))
    readme = SOURCE_ROOT / "tools/relay_engine/README.md"
    entries.append((readme, "relay_engine/README.md"))
    standalone = SOURCE_ROOT / "tools/relay-lint.py"
    entries.append((standalone, "tools/relay-lint.py"))

    installed_markdown = [
        path for pattern in ("skills/**/*.md",
                             "tools/relay-lint-fixtures/**/*.md")
        for path in SOURCE_ROOT.glob(pattern) if path.is_file()
    ]
    for plugin in ("adt-master", "adt-orchestrator", "adt-pair"):
        prefix = "plugins/" + plugin + "/"
        entries.append((standalone, prefix + "tools/relay-lint.py"))
        for source in installed_markdown:
            entries.append((source, prefix +
                            source.relative_to(SOURCE_ROOT).as_posix()))
    return sorted(entries, key=lambda entry: entry[1].encode("utf-8"))


def installed_markdown():
    return [(destination, source) for source, destination in ruled_manifest()
            if destination.endswith(".md")]


def _occurrences(pattern, values):
    expression = re.compile(pattern, re.IGNORECASE)
    return [(name, match.group(0)) for name, value in values
            for match in expression.finditer(value)]


def census_values():
    parser = cli.build_parser()
    synthetic = _subparsers(parser).add_parser(
        "synthetic-census-probe", help="SQLite synthetic sentinel",
        description="SQLite synthetic sentinel")
    synthetic.set_defaults(handler=lambda _args: 0)
    synthetic_help = help_population(parser)
    if not _occurrences(r"sqlite", synthetic_help):
        raise AssertionError("synthetic command escaped parser enumeration")

    markdown = [(destination, source.read_text(encoding="utf-8"))
                for destination, source in installed_markdown()]
    inventory = sorted(strings.INVENTORY.items())
    help_text = help_population(cli.build_parser())
    engine_readme = [("tools/relay_engine/README.md",
                      (SOURCE_ROOT / "tools/relay_engine/README.md")
                      .read_text(encoding="utf-8"))]
    engine_surface = engine_readme + inventory + help_text
    all_surface = markdown + inventory + help_text

    matches = {
        "sqlite": _occurrences(r"sqlite", all_surface),
        "database": _occurrences(r"database", engine_surface),
        "ledger": _occurrences(r"ledger", engine_surface),
        "registration": _occurrences(r"\bauth\b|\btoken\b", engine_surface),
        "d25": _occurrences(r"fallback|authoring mode", engine_surface),
        "overclaim": _occurrences(
            r"credential|tamper-proof|prevents|guarantees exclusivity",
            engine_surface),
        "hand_relay": _occurrences(
            r"hand(?:-|[ \t]+)?relay", engine_surface),
    }
    for name in ("sqlite", "database", "ledger", "registration", "d25",
                 "overclaim"):
        if matches[name]:
            raise AssertionError("%s matches: %r" % (name, matches[name]))
    if matches["hand_relay"] != [
            ("error-daemon-down-remedy", "Hand-relay")]:
        raise AssertionError("hand-relay census mismatch: %r" %
                             (matches["hand_relay"],))
    if errors.E_DAEMON_DOWN_ESCALATION != ESCALATION:
        raise AssertionError("daemon-down escalation bytes changed")

    encoded_inventory = json.dumps(
        dict(inventory), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "markdown": [name for name, _ in markdown],
        "inventory_sha256": hashlib.sha256(encoded_inventory).hexdigest(),
        "commands": [name for name, _ in help_text],
        "matches": matches,
    }


def write_census_artifact():
    values = census_values()
    run_id = os.environ.get("RELAY_ENGINE_MATRIX_RUN")
    results_root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
    if not run_id or not results_root:
        return None, values
    lines = ["run " + run_id]
    lines.extend("manifest_md=" + path for path in values["markdown"])
    lines.append("inventory_sha256=" + values["inventory_sha256"])
    lines.extend("command=" + command for command in values["commands"])
    lines.extend(ALLOWED_SITE_FIELDS)
    lines.extend((
        "gate_status=pass",
        "sqlite_matches=0",
        "database_matches=0",
        "ledger_matches=0",
        "result=zero forbidden matches",
    ))
    body = ("\n".join(lines) + "\n").encode("utf-8")
    path = Path(results_root, "v29-engine-nondisclosure-census.txt")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("zero-progress census write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path, values


def _relay_body(role, phase, authority, dispatch, sender, recipient, *,
                parent=None, extra="", body="fixture body"):
    parent_line = ("" if parent is None else
                   "PARENT_DISPATCH_ID: %s\n" % parent)
    return ("## relay\n\n"
            "ROLE: %s\n"
            "PHASE: %s\n"
            "AUTHORITY: %s\n"
            "DISPATCH_ID: %s\n"
            "%s"
            "RUN_ID: e2e\n"
            "CEREMONY_TIER: medium\n"
            "EVIDENCE_TARGET: E2\n"
            "HUMAN_GATE_REQUIRED: no\n"
            "FROM: %s\n"
            "TO: %s\n"
            "%s"
            "SUBJECT: e2e chain fixture\n\n"
            "%s\n\n"
            "FINAL_GIT_STATUS_SHORT: none — fixture root only\n" % (
                role, phase, authority, dispatch, parent_line,
                sender, recipient, extra, body))


class RelayProcess:
    def __init__(self, relay, environment, cwd):
        self.relay = [os.fspath(part) for part in relay]
        self.environment = dict(environment)
        self.cwd = os.fspath(cwd)
        self.log = []

    def run(self, *args, json_result=False, check=True):
        command = self.relay + list(args)
        detached_start = tuple(args[:2]) == ("daemon", "start")
        result = subprocess.run(
            command, cwd=self.cwd, env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=(subprocess.DEVNULL if detached_start else subprocess.PIPE),
            stderr=(subprocess.DEVNULL if detached_start else subprocess.PIPE),
            text=True, timeout=20, check=False)
        stdout = "" if result.stdout is None else result.stdout
        stderr = "" if result.stderr is None else result.stderr
        self.log.append({"command": command, "status": result.returncode,
                         "stdout": stdout, "stderr": stderr})
        if check and result.returncode != 0:
            raise AssertionError(
                "command failed (%d): %r\nstdout=%s\nstderr=%s" % (
                    result.returncode, command, stdout, stderr))
        if json_result:
            return json.loads(stdout)
        return result


def run_chain(process, root, cid=None, did=None):
    root = Path(root)
    drafts = root / "drafts"
    drafts.mkdir()
    process.run("daemon", "start", "--root", os.fspath(root),
                "--run-id", "e2e", "--seat",
                "e2e.orchestrator-planner")
    if cid is not None:
        reported = process.run("version", json_result=True)
        actual_cid = {
            "kit": reported["kit"], "fp": reported["fingerprint"],
            "install": reported["install"],
        }
        if actual_cid != cid:
            raise AssertionError("client identity plumbing mismatch")
    if did is not None:
        state = json.loads(Path(root, ".engine/daemon.json").read_text())
        if state.get("identity") != did:
            raise AssertionError("daemon identity plumbing mismatch")
    try:
        planner = process.run(
            "seat", "register", "e2e.pair.planner", "--role", "Planner",
            "--dispatch", "e2e-pair-boot", "--root", os.fspath(root),
            json_result=True)
        implementer = process.run(
            "seat", "register", "e2e.pair.implementer", "--role",
            "Implementer", "--dispatch", "e2e-pair-boot", "--root",
            os.fspath(root), json_result=True)
        ruling = process.run(
            "seat", "register", "e2e.ruling.orchestrator-planner",
            "--role", "Orchestrator Planner", "--dispatch",
            "e2e-ruling-boot", "--root", os.fspath(root),
            json_result=True)

        def submit(name, text, key, edge=None):
            path = drafts / name
            path.write_text(text, encoding="utf-8")
            command = ["submit", os.fspath(path), "--root", os.fspath(root),
                       "--key", os.fspath(root / key)]
            if edge is not None:
                command.extend(("--admits-against", edge))
            return process.run(*command, json_result=True)

        plan = submit(
            "plan.md", _relay_body(
                "Planner", "PLAN", "plan-only", "e2e-plan",
                "e2e.pair.planner", "e2e.pair.implementer",
                extra="DELEGATED_DISPATCH_AUTHORITY: yes\n"),
            planner["key_path"])
        review = submit(
            "plan-review.md", _relay_body(
                "Implementer", "PLAN-REVIEW", "review-only",
                "e2e-plan-review", "e2e.pair.implementer",
                "e2e.pair.planner", parent="e2e-plan",
                body="VERDICT: approve"), implementer["key_path"])
        implementation = submit(
            "implementation.md", _relay_body(
                "Planner", "PLAN", "plan-only", "e2e-impl",
                "e2e.pair.planner", "e2e.pair.implementer",
                parent="e2e-plan-review",
                extra="DELEGATED_DISPATCH_AUTHORITY: yes\n",
                body=("SCOPE_DIFF:\n"
                      "- tools/relay_engine/** -> in\n"
                      "SCOPE_DIFF_RESULT: all-in\n\n"
                      "DISPATCH IMPL")), planner["key_path"])
        superseding = submit(
            "ruling.md", _relay_body(
                "Orchestrator Planner", "PLAN", "plan-only",
                "e2e-ruling", "e2e.ruling.orchestrator-planner",
                "e2e.pair.planner",
                extra="SUPERSEDES: %s\n" % plan["path"]),
            ruling["key_path"])
        if len({plan["path"], review["path"], implementation["path"],
                superseding["path"]}) != 4:
            raise AssertionError("chain paths must be distinct")

        process.run("daemon", "stop", "--root", os.fspath(root))
        bypass_lane = root / "bypass"
        bypass_lane.mkdir()
        rendered_stamps = [
            datetime.strptime(path.stem.rsplit("-", 2)[-2] + "-" +
                              path.stem.rsplit("-", 2)[-1],
                              "%Y%m%d-%H%M%S")
            for path in root.rglob("*.md")
            if re.search(r"-\d{8}-\d{6}$", path.stem)
        ]
        next_stamp = max(rendered_stamps) + timedelta(seconds=1)
        while datetime.now() < next_stamp:
            time.sleep(0.05)
        bypass_stamp = datetime.now().replace(microsecond=0)
        bypass = bypass_lane / (
            "PLAN-planner-%s.md" % bypass_stamp.strftime("%Y%m%d-%H%M%S"))
        bypass.write_text(_relay_body(
            "Planner", "PLAN", "plan-only", "e2e-bypass",
            "e2e.pair.planner", "e2e.pair.implementer"), encoding="utf-8")

        process.run("daemon", "start", "--root", os.fspath(root),
                    "--run-id", "e2e", "--seat",
                    "e2e.orchestrator-planner")
        reconciled = process.run(
            "reconcile", "--root", os.fspath(root), json_result=True)
        expected = {
            "path": "bypass/" + bypass.name,
            "origin": "hand",
            "advisories": ["hand-authored-import"],
        }
        if expected not in reconciled.get("hand", []):
            raise AssertionError("bypass was not surfaced as flagged hand input")
        verified = process.run(
            "verify", "--root", os.fspath(root), json_result=True)
        if not verified["ok"]:
            raise AssertionError("verification did not finish green")
        linted = process.run(
            "lint", "--relay-root", os.fspath(root), json_result=True)
        if linted[os.fspath(root)]["errors"]:
            raise AssertionError("rendered relay tree did not lint green")
        return {"plan": plan, "review": review,
                "implementation": implementation,
                "superseding": superseding, "bypass": expected}
    finally:
        process.run("daemon", "stop", "--root", os.fspath(root),
                    check=False)


class TestE2EChain(unittest.TestCase):
    def test_handoff_chain_reconcile_and_lint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "root")
            root.mkdir()
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.fspath(SOURCE_ROOT / "tools")
            process = RelayProcess(
                [PYTHON, SOURCE_ROOT / "tools/relay"], environment,
                Path(temporary))
            result = run_chain(process, root, CID, DID)
            self.assertEqual(result["review"]["advisories"], [])
            self.assertEqual(result["bypass"]["origin"], "hand")


class TestCensus(unittest.TestCase):
    def test_help_population_includes_root_surface(self):
        parser = cli.build_parser()
        subparsers = _subparsers(parser)
        subparsers.add_parser(
            "synthetic-root-census-probe",
            help="sQlItE root census sentinel")
        self.assertIn("sQlItE root census sentinel", parser.format_help())
        self.assertEqual(
            _occurrences(r"sqlite", help_population(parser)),
            [("<root>", "sQlItE")])

        root_only = argparse.ArgumentParser(
            description="LeDgEr root-only census sentinel")
        self.assertEqual(
            _occurrences(r"ledger", help_population(root_only)),
            [("<root>", "LeDgEr")])

    def test_shipped_text_sweep(self):
        _, values = write_census_artifact()
        for name in ("registration", "d25", "overclaim"):
            self.assertEqual(values["matches"][name], [])
        self.assertEqual(values["matches"]["hand_relay"], [
            ("error-daemon-down-remedy", "Hand-relay")])

    def test_nondisclosure_census(self):
        path, values = write_census_artifact()
        for name in ("sqlite", "database", "ledger"):
            self.assertEqual(values["matches"][name], [])
        if path is not None:
            self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0],
                             "run " + os.environ["RELAY_ENGINE_MATRIX_RUN"])

    def test_census_artifact_schema(self):
        path, _ = write_census_artifact()
        if path is None:
            self.skipTest("durable results environment is not set; export "
                          "RELAY_ENGINE_RESULTS_ROOT and RELAY_ENGINE_MATRIX_RUN "
                          "(see the AGENTS.md engine suite entry point)")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0],
                         "run " + os.environ["RELAY_ENGINE_MATRIX_RUN"])
        for field in ALLOWED_SITE_FIELDS:
            self.assertEqual(lines.count(field), 1)
        self.assertIn("gate_status=pass", lines)
        self.assertIn("result=zero forbidden matches", lines)


if __name__ == "__main__":
    unittest.main()
