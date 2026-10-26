import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import xrootfixgen

from relay_engine import rules
from relay_engine.envelope import REQUIRED_FIELDS


TOOLS = Path(__file__).parents[2]
REPO = TOOLS.parent
FIXTURES = TOOLS / "relay-engine-fixtures"
PARITY = FIXTURES / "parity"
MANIFEST = PARITY / "corpus-manifest.json"
GOLDEN = PARITY / "golden-verdicts.json"
SCRIPT = TOOLS / "relay-lint.py"


def _standalone():
    spec = importlib.util.spec_from_file_location(
        "v29_frozen_relay_lint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _population():
    roots = [TOOLS / "relay-lint-fixtures", FIXTURES / "corpora"]
    return sorted(path for root in roots for path in root.rglob("*")
                  if path.is_file())


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_process_violations(source):
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        base = node.func.value
        if not isinstance(base, ast.Name):
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        function = owner.name if owner is not None else None
        if base.id == "subprocess" and not (
                node.func.attr == "Popen" and function == "_xroot_start_git"):
            violations.append("subprocess call outside Git seam")
        if base.id == "os" and (node.func.attr in {"system", "popen"} or
                               node.func.attr.startswith(("exec", "spawn", "posix_spawn", "fork"))):
            violations.append("os process launch")
    return violations


def non_git_commands(commands):
    return [command for command in commands if not command or command[0] != "git"]


def _verdict(result):
    return {"errors": sorted(result.errors),
            "warnings": sorted(result.warnings),
            "exit": 1 if result.errors else 0}


def _collect(module):
    single = {}
    for path in _population():
        if path.suffix == ".md":
            single[str(path.relative_to(REPO))] = _verdict(
                module.lint_file(path, freshness=False))
    root_paths = [TOOLS / "relay-lint-fixtures"]
    root_paths.extend(sorted(
        path for path in (FIXTURES / "corpora").iterdir()
        if path.is_dir()))
    roots = {str(path.relative_to(REPO)): _verdict(
        module.lint_relay_root(path)) for path in root_paths}
    indexes = {str(path.relative_to(REPO)): _verdict(
        module.lint_relay_index(path)) for path in _population()
        if path.name == "INDEX.md"}
    file_root = {}
    plancontract = TOOLS / "relay-lint-fixtures" / "plancontract"
    for member in sorted(plancontract.iterdir()):
        if member.is_dir():
            artifact_root = member / ".relays" / "v29"
            for path in sorted(member.rglob("*.md")):
                file_root[str(path.relative_to(REPO))] = _verdict(
                    module.lint_file(path, freshness=False,
                                     artifact_root=artifact_root))
    return {"single": single, "root": roots, "index": indexes,
            "file_root": file_root}


def generate_artifacts():
    PARITY.mkdir(parents=True, exist_ok=True)
    files = {str(path.relative_to(REPO)): _digest(path)
             for path in _population()}
    manifest = {"count": len(files), "files": files}
    MANIFEST.write_text(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")) + "\n")
    golden = {
        "producer_sha256": _digest(SCRIPT),
        "manifest_sha256": _digest(MANIFEST),
        "verdicts": _collect(_standalone()),
    }
    GOLDEN.write_text(json.dumps(
        golden, sort_keys=True, separators=(",", ":")) + "\n")


class TestParity(unittest.TestCase):
    def test_manifest_is_complete_and_content_bound(self):
        manifest = json.loads(MANIFEST.read_text())
        actual = {str(path.relative_to(REPO)): _digest(path)
                  for path in _population()}
        self.assertTrue(actual)
        self.assertEqual(manifest, {"count": len(actual), "files": actual})

    def test_native_required_fields_are_submission_contract(self):
        self.assertEqual(tuple(rules.REQUIRED_FIELDS),
                         tuple(REQUIRED_FIELDS))

    def test_native_blocked_script_leg_matches_frozen_golden(self):
        source = Path(rules.__file__).read_text()
        self.assertNotIn("importlib", source)
        self.assertEqual(native_process_violations(source), [])
        self.assertNotIn("relay-lint.py", source)
        with tempfile.TemporaryDirectory() as isolated:
            self.assertFalse(Path(isolated, "relay-lint.py").exists())
            actual = _collect(rules)
        frozen = json.loads(GOLDEN.read_text())
        self.assertEqual(_digest(SCRIPT), frozen["producer_sha256"])
        self.assertEqual(_digest(MANIFEST), frozen["manifest_sha256"])
        self.assertEqual(actual, frozen["verdicts"])

    def test_native_process_boundary_rejects_other_launches(self):
        source = Path(rules.__file__).read_text()
        self.assertEqual(native_process_violations(source), [])
        for added, expected in (
                ("subprocess.run(['python3', 'relay-lint.py'])", "subprocess call outside Git seam"),
                ("os.posix_spawnp('python3', ['python3', 'relay-lint.py'], {})", "os process launch")):
            with self.subTest(added=added):
                self.assertEqual(native_process_violations(source + "\n" + added), [expected])

    def test_native_cross_root_launches_only_git(self):
        commands = []
        original = rules._xroot_start_git

        def observed(command, **kwargs):
            commands.append(list(command))
            return original(command, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            label, root, expected_exit, expected_errors = xrootfixgen._build_case(
                Path(directory), "XV-valid-edge", expected_errors=[])
            self.assertEqual(label, "xroot/XV-valid-edge")
            self.assertEqual(expected_exit, 0)
            with rules.xroot_fact_cache(rules.XrootFactCache()), \
                    mock.patch.object(rules, "_xroot_start_git", side_effect=observed):
                result = rules.lint_relay_root(root)
            self.assertEqual(result.errors, expected_errors)
        self.assertTrue(any("--batch" in command for command in commands))
        self.assertTrue(any("--batch" not in command for command in commands))
        self.assertEqual(non_git_commands(commands), [])
        forbidden = [sys.executable, str(SCRIPT)]
        self.assertEqual(non_git_commands(commands + [forbidden]), [forbidden])

    def test_live_secondary_comparison_all_modes(self):
        live = _collect(_standalone())
        native = _collect(rules)
        self.assertEqual(native, live)
        root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
        if root:
            destination = Path(root, "v29-engine-parity-verdicts.json")
            destination.write_text(json.dumps({
                "manifest_sha256": _digest(MANIFEST),
                "producer_sha256": _digest(SCRIPT),
                "status": "PASS", "verdicts": native,
            }, sort_keys=True, separators=(",", ":")) + "\n")

    def test_live_secondary_comparison_explicit_order_inversion(self):
        source = TOOLS / "relay-lint-fixtures" / "t11closure" / \
            "R32-singleton-unstamped-candidate"
        with tempfile.TemporaryDirectory() as isolated:
            fixture = Path(isolated, "R32-singleton-unstamped-candidate")
            shutil.copytree(source, fixture)
            for stamp, path in enumerate(
                    sorted(fixture.glob("*.md"), reverse=True), start=1):
                os.utime(path, ns=(stamp, stamp))
            native = _verdict(rules.lint_relay_root(fixture))
            live = _verdict(_standalone().lint_relay_root(fixture))
        self.assertEqual(native, live)


if os.environ.get("RELAY_REGENERATE_PARITY") == "1":
    generate_artifacts()


if __name__ == "__main__":
    unittest.main()
